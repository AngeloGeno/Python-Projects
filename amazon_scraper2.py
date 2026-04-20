import asyncio
import csv
import json
import random
import re
from datetime import datetime
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

# ─────────────────────────── CONFIG ───────────────────────────
BASE_URL    = "https://www.amazon.com"
SEARCH_URL  = "https://www.amazon.com/s?k=instant+coffee"
MAX_PAGES   = 3
OUTPUT_FILE = "amazon_instant_coffee.csv"
CONCURRENCY = 2          # keep low to avoid triggering CAPTCHA
RETRY_LIMIT = 3
HEADLESS    = False      # visible browser is harder to detect

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]


# ─────────────────────────── UTILS ────────────────────────────
async def random_delay(min_sec: float = 2.0, max_sec: float = 5.0) -> None:
    await asyncio.sleep(random.uniform(min_sec, max_sec))


def is_blocked(html: str) -> bool:
    markers = [
        "captcha",
        "are you human",
        "enter the characters you see below",
        "unusual traffic",
        "cf-browser-verification",
        "robot check",
    ]
    lower = html.lower()
    return any(m in lower for m in markers)


def extract_asin(url: str) -> str | None:
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


async def new_stealth_page(context):
    page = await context.new_page()
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
        const orig = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : orig(p);
    """)
    return page


async def safe_goto(page, url: str) -> bool:
    for attempt in range(RETRY_LIMIT):
        try:
            await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            await asyncio.sleep(3)   # let JS settle

            html = await page.content()
            if is_blocked(html):
                print(f"   ⚠️  CAPTCHA/block on attempt {attempt + 1} — waiting …")
                await asyncio.sleep(random.uniform(8, 15))
                continue
            return True
        except PWTimeout:
            print(f"   ⏱  Timeout attempt {attempt + 1}: {url}")
        except Exception as exc:
            print(f"   ❌ Error attempt {attempt + 1}: {exc}")
        await asyncio.sleep(2 ** attempt)
    return False


# ────────────────────────── PARSING ───────────────────────────
def parse_search_page(html: str) -> list[str]:
    """Extract product URLs from an Amazon search results page."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[str] = []

    for a in soup.select("a.a-link-normal[href]"):
        href = a["href"]
        if "/dp/" not in href:
            continue
        full = urljoin(BASE_URL, href.split("?")[0])   # drop query string
        asin = extract_asin(full)
        if asin and asin not in seen:
            seen.add(asin)
            links.append(full)

    return links


def parse_next_page(html: str) -> str | None:
    """Return the next-page URL or None."""
    soup = BeautifulSoup(html, "html.parser")
    btn = soup.select_one("a.s-pagination-next")
    if btn and btn.get("href"):
        return urljoin(BASE_URL, btn["href"])
    return None


def parse_product(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def text(sel: str) -> str:
        el = soup.select_one(sel)
        return el.get_text(strip=True) if el else "N/A"

    # ── Name ──────────────────────────────────────────────────
    name = text("#productTitle")

    # ── Price ─────────────────────────────────────────────────
    price = "N/A"
    for sel in ("#priceblock_ourprice", "#priceblock_dealprice",
                "span.a-price > span.a-offscreen",
                ".a-price .a-price-whole"):
        el = soup.select_one(sel)
        if el:
            raw = el.get_text(strip=True)
            cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
            if cleaned:
                price = cleaned
                break

    # ── Brand ─────────────────────────────────────────────────
    brand = "N/A"
    # Method 1: byline
    byline = soup.select_one("#bylineInfo")
    if byline:
        brand = byline.get_text(strip=True).replace("Brand:", "").replace("Visit the", "").replace("Store", "").strip()
    # Method 2: product details table
    if brand == "N/A":
        for row in soup.select("#productDetails_techSpec_section_1 tr, #detailBullets_feature_div li"):
            t = row.get_text(" ", strip=True)
            if "brand" in t.lower():
                parts = t.split(":")
                if len(parts) >= 2:
                    brand = parts[-1].strip()
                    break
    # Method 3: JSON-LD
    if brand == "N/A":
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
                b = data.get("brand", {})
                brand = b.get("name", "N/A") if isinstance(b, dict) else str(b)
                if brand != "N/A":
                    break
            except Exception:
                pass

    # ── Rating ────────────────────────────────────────────────
    rating = "N/A"
    r_el = soup.select_one("span[data-hook='rating-out-of-text'], #acrPopover span.a-size-base")
    if r_el:
        rating = r_el.get_text(strip=True).split()[0]

    # ── Review count ──────────────────────────────────────────
    review_count = "N/A"
    rc_el = soup.select_one("#acrCustomerReviewText, span[data-hook='total-review-count']")
    if rc_el:
        review_count = re.sub(r"[^\d]", "", rc_el.get_text())

    # ── Weight ────────────────────────────────────────────────
    weight = "N/A"
    for row in soup.select("#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr"):
        t = row.get_text(" ", strip=True)
        if any(k in t.lower() for k in ["weight", "volume", "size", "item dimensions"]):
            tds = row.find_all("td")
            if len(tds) >= 2:
                weight = tds[-1].get_text(strip=True)
                break

    # ── Stock ─────────────────────────────────────────────────
    page_text = soup.get_text().lower()
    if "currently unavailable" in page_text or "out of stock" in page_text:
        stock = "Out of Stock"
    elif "add to cart" in page_text or "add to basket" in page_text:
        stock = "In Stock"
    else:
        stock = "Unknown"

    return {
        "asin":         extract_asin(url) or "N/A",
        "name":         name,
        "brand":        brand,
        "price_usd":    price,
        "weight":       weight,
        "rating":       rating,
        "review_count": review_count,
        "stock":        stock,
        "url":          url,
        "scraped_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ─────────────────────────── SCRAPING ─────────────────────────
async def collect_product_urls(browser) -> list[str]:
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )
    page = await new_stealth_page(context)

    all_urls: list[str] = []
    seen_asins: set[str] = set()
    next_url: str | None = SEARCH_URL

    for page_num in range(1, MAX_PAGES + 1):
        if not next_url:
            break

        print(f"📄 Search page {page_num}: {next_url}")
        ok = await safe_goto(page, next_url)
        if not ok:
            print("   ↳ Gave up.")
            break

        html = await page.content()

        # DEBUG
        with open("debug_amazon.html", "w", encoding="utf-8") as f:
            f.write(html)
        _soup = BeautifulSoup(html, "html.parser")
        _all_a = _soup.find_all("a", href=True)
        _dp    = [a for a in _all_a if "/dp/" in a.get("href", "")]
        print(f"   [DEBUG] title : {_soup.title.string if _soup.title else 'NONE'}")
        print(f"   [DEBUG] <a> total={len(_all_a)}  /dp/ links={len(_dp)}")
        if _dp:
            print(f"   [DEBUG] sample /dp/ href: {_dp[0]['href'][:80]}")
        else:
            print(f"   [DEBUG] body text preview: {_soup.get_text(' ', strip=True)[:300]}")
        print(f"   [DEBUG] full HTML saved → debug_amazon.html")

        links = parse_search_page(html)

        new_links = []
        for link in links:
            asin = extract_asin(link) or link
            if asin not in seen_asins:
                seen_asins.add(asin)
                new_links.append(link)

        print(f"   ↳ {len(new_links)} new products (total {len(all_urls) + len(new_links)})")
        all_urls.extend(new_links)

        next_url = parse_next_page(html)
        await random_delay(2, 4)

    await context.close()
    return all_urls


async def scrape_product(context, url: str, semaphore: asyncio.Semaphore) -> dict | None:
    async with semaphore:
        page = await new_stealth_page(context)
        try:
            ok = await safe_goto(page, url)
            if not ok:
                return None

            await random_delay(1.5, 3.5)
            data = parse_product(await page.content(), url)
            print(f"   ✔  {data['name'][:60]}  |  ${data['price_usd']}")
            return data

        except Exception as exc:
            print(f"   ❌ {url} → {exc}")
            return None
        finally:
            await page.close()


# ──────────────────────────── MAIN ────────────────────────────
async def scrape() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--start-maximized",
            ],
        )

        print("🚀 Collecting product URLs …")
        product_urls = await collect_product_urls(browser)
        print(f"✅ {len(product_urls)} unique products found\n")

        if not product_urls:
            print("Nothing to scrape. Exiting.")
            await browser.close()
            return

        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        semaphore = asyncio.Semaphore(CONCURRENCY)
        tasks = [scrape_product(context, url, semaphore) for url in product_urls]

        print(f"⚙️  Scraping {len(tasks)} products (concurrency={CONCURRENCY}) …\n")
        results = [r for r in await asyncio.gather(*tasks) if r]

        await browser.close()

    if not results:
        print("No data collected.")
        return

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    print(f"\n✨ Saved {len(results)} products → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(scrape())
