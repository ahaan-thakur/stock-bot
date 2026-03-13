"""
Stock checker bot
==================
Site 1 — karzanddolls.com  (9 category listing pages)
  Behaviour : cards vanish when OOS, "coming soon" badge before launch
  Alert on  : new card, coming soon → add to cart, reappeared card

Site 2 — diecastsilkroad.com  (paginated /collections/all)
  Behaviour : all items always listed, stock shown via button text
  Alert on  : item with fingerprint not seen before that is in stock
              (catches new stock even when total count is unchanged,
               because sold items and genuinely new items have different
               fingerprints built from name + price + variant URL)

Fingerprint = MD5(name + price + variant_path)
  Same item relisted  → same fingerprint → no duplicate alert
  Different new item  → new fingerprint  → alert fires

State persisted in state/state.json, committed back to repo each run.

Secrets required (GitHub repository secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os, json, time, random, hashlib, logging, requests
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent / "state" / "state.json"
BASE_DSR   = "https://diecastsilkroad.com"

# ── karzanddolls: all 9 category pages to watch ───────────────────────────────
KARZANDDOLLS_CATEGORIES = [
    ("Mainlines",        "https://www.karzanddolls.com/details/hot+wheels/mainlines/MTEw"),
    ("Pop Culture",      "https://www.karzanddolls.com/details/hot+wheels/pop-culture/MTE5"),
    ("Card Art Premiums","https://www.karzanddolls.com/details/hot+wheels/card-art-premiums/MTE0"),
    ("Gift Pack",        "https://www.karzanddolls.com/details/hot+wheels/gift-pack/MTE2"),
    ("Car Culture",      "https://www.karzanddolls.com/details/hot+wheels/car-culture/MTEx"),
    ("Boulevard Series", "https://www.karzanddolls.com/details/hot+wheels/boulevard-series/MTIw"),
    ("Team Transport",   "https://www.karzanddolls.com/details/hot+wheels/team-transport/MTQz"),
    ("Mini GT",          "https://www.karzanddolls.com/details/mini+gt+/mini-gt/MTY1"),
    ("Pop Race",         "https://www.karzanddolls.com/details/pop+race/pop-race/MTc0"),
]

# ── HTTP ───────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def fetch(url: str, retries: int = 3) -> Optional[str]:
    """Fetch a URL with retry + exponential backoff.
    503/429 (rate-limited) → wait and retry. Other errors → fail immediately.
    """
    for attempt in range(1, retries + 1):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
            return r.text
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (503, 429) and attempt < retries:
                wait = 15 * attempt   # 15s on attempt 1, 30s on attempt 2
                log.warning(f"  {status} on attempt {attempt} — retrying in {wait}s...")
                time.sleep(wait)
                continue
            log.error(f"Fetch failed [{url}]: {e}")
            return None
        except Exception as e:
            log.error(f"Fetch failed [{url}]: {e}")
            return None

def jitter():
    t = random.uniform(1, 3)
    log.info(f"  Waiting {t:.1f}s...")
    time.sleep(t)


# ── Fingerprint ────────────────────────────────────────────────────────────────

def fingerprint(*parts: str) -> str:
    raw = "|".join(p.lower().strip() for p in parts if p)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ── Telegram ───────────────────────────────────────────────────────────────────

def alert(token: str, chat_id: str, emoji: str, headline: str,
          category: str, name: str, url: str):
    text = (
        f"{emoji} <b>{headline}</b>\n"
        f"<i>{category}</i>\n\n"
        f"{name}\n\n"
        f'<a href="{url}">View product</a>'
    )
    if not token or not chat_id:
        log.info(f"[ALERT — no Telegram] {headline}: {name}")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=10,
        )
        r.raise_for_status()
        log.info(f"  Telegram sent: {headline} — {name}")
    except Exception as e:
        log.error(f"  Telegram failed: {e}")


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"Could not read state: {e} — starting fresh.")
    return {"karzanddolls": {}, "diecastsilkroad": {}}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    log.info(f"State saved → {STATE_FILE}")


# ══════════════════════════════════════════════════════════════════════════════
#  SITE 1 — karzanddolls.com
#  Card structure (confirmed from live page):
#    Each product is wrapped in a col/card block containing:
#      - <h3> or heading with product name
#      - Price text (Rs. XXXX)
#      - "ADD TO CART" button text  → status: buyable
#      - "COMING SOON" badge text   → status: soon
#      - "New Arrival" badge
#    When a page has no products: text contains
#      "Currently There is no Product Here"
#    Product URL: <a href="/product/category/slug/hash">
# ══════════════════════════════════════════════════════════════════════════════

def kd_card_status(text: str) -> str:
    t = text.lower()
    if "add to cart" in t:
        return "buyable"
    if "coming soon" in t:
        return "soon"
    return "unknown"

def kd_parse_cards(html: str, base_url: str) -> dict:
    """
    Returns {fingerprint: {name, url, status}} for all product cards.
    Skips the empty-state page gracefully.
    """
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(separator=" ").lower()

    if "currently there is no product here" in page_text:
        return {}

    cards = {}

    # Products are in anchor tags pointing to /product/... paths
    # Each product block contains a heading (product name) + price + button
    # Strategy: find all links to /product/ and walk up to the card container
    seen_hrefs = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/product/" not in href:
            continue
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        # Walk up to find the card container (has price + button text)
        container = a
        for _ in range(6):  # walk up max 6 levels
            parent = container.parent
            if parent is None:
                break
            parent_text = parent.get_text(separator=" ", strip=True).lower()
            if "rs." in parent_text and ("add to cart" in parent_text or "coming soon" in parent_text):
                container = parent
                break
            container = parent

        card_text = container.get_text(separator=" ", strip=True)

        # Extract product name — find the most prominent text near the link
        name = ""
        for tag in ["h3", "h4", "h2", "strong", "p"]:
            el = container.find(tag)
            if el:
                t = el.get_text(strip=True)
                if len(t) > 4 and "rs." not in t.lower():
                    name = t[:120]
                    break
        if not name:
            name = a.get_text(strip=True)[:120] or href.split("/")[-1].replace("-", " ").title()[:80]

        # Extract price
        price = ""
        for el in container.find_all(string=True):
            if "rs." in el.lower():
                price = el.strip()[:20]
                break

        full_url = href if href.startswith("http") else urljoin(base_url, href)
        fid = fingerprint(name, price)
        status = kd_card_status(card_text)
        cards[fid] = {"name": name, "url": full_url, "status": status, "gone": False}

    return cards

def check_karzanddolls(state: dict, token: str, chat_id: str) -> dict:
    prev_all: dict = state.get("karzanddolls", {})
    new_all:  dict = {}

    for cat_name, cat_url in KARZANDDOLLS_CATEGORIES:
        log.info(f"[KarzAndDolls] Checking: {cat_name}")
        html = fetch(cat_url)
        if html is None:
            log.warning(f"  Fetch failed — skipping {cat_name}.")
            jitter()
            continue

        current = kd_parse_cards(html, cat_url)
        log.info(f"  Found {len(current)} card(s).")

        # Merge current cards into new_all, carry forward gone cards
        for fid, item in current.items():
            new_all[fid] = {**item, "category": cat_name}

        prev = {fid: v for fid, v in prev_all.items() if v.get("category") == cat_name}

        # Mark cards that disappeared as gone
        for fid, prev_item in prev.items():
            if fid not in current and not prev_item.get("gone", False):
                log.info(f"  Gone: {prev_item['name']}")
                new_all[fid] = {**prev_item, "gone": True}
            elif fid not in current:
                new_all[fid] = prev_item  # keep existing gone record

        # Diff and alert
        for fid, item in current.items():
            name, url, status = item["name"], item["url"], item["status"]
            prev_item = prev.get(fid)

            if prev_item is None:
                # Brand new card never seen before
                log.info(f"  NEW [{status}]: {name}")
                if status == "buyable":
                    alert(token, chat_id, "🛒", "New product — buy now!", cat_name, name, url)
                else:
                    alert(token, chat_id, "👀", "New product listed", cat_name, name, url)

            elif prev_item.get("gone", False):
                # Was gone, now reappeared
                log.info(f"  REAPPEARED [{status}]: {name}")
                if status == "buyable":
                    alert(token, chat_id, "🔄", "Back in stock — buy now!", cat_name, name, url)
                else:
                    alert(token, chat_id, "🔄", "Back — coming soon", cat_name, name, url)

            elif prev_item.get("status") != "buyable" and status == "buyable":
                # coming soon / unknown → now buyable
                log.info(f"  NOW BUYABLE: {name}")
                alert(token, chat_id, "🛒", "Now available to buy!", cat_name, name, url)

            else:
                log.info(f"  No change [{status}]: {name}")

        jitter()

    state["karzanddolls"] = new_all
    return state


# ══════════════════════════════════════════════════════════════════════════════
#  SITE 2 — diecastsilkroad.com  (Shopify store)
#  Card structure (confirmed from live page):
#    Products listed as <li> items in a grid.
#    Each card has:
#      - Product name in <h3> (or <h2>) heading
#      - Link: <a href="/products/slug?variant=XXXXXXX">
#      - Price: "Rs. XXX.00"
#      - Stock badge: <span class="add-to-cart-text__content">
#          "Add to cart"  → in stock
#          "Sold out"     → out of stock
#    Pagination: ?page=2, ?page=3 ...
#    Total item count shown as "365 items" on the page.
#
#  Strategy:
#    - Scrape pages sorted by newest (sort_by=created-descending)
#    - Stop paginating once we've seen a page where ALL items are already
#      in our state (means we've caught up to previously seen stock)
#    - Fingerprint = MD5(name + price + variant_path)
#      variant_path is the /products/slug?variant=ID portion — this ensures
#      two variants of the same product get distinct fingerprints
# ══════════════════════════════════════════════════════════════════════════════

DSR_LISTING = "https://diecastsilkroad.com/collections/all"
DSR_PARAMS  = "sort_by=created-descending"  # newest first

def dsr_parse_page(html: str) -> list[dict]:
    """Parse one page of the DSR listing. Returns list of item dicts."""
    soup = BeautifulSoup(html, "lxml")
    items = []

    # Each product is in a <li> with a link to /products/
    for li in soup.find_all("li"):
        a = li.find("a", href=lambda h: h and "/products/" in h)
        if not a:
            continue

        href = a["href"]  # e.g. /products/tomica-ferrari?variant=123
        full_url = href if href.startswith("http") else urljoin(BASE_DSR, href)

        # Name — in the <h3> (or <h2>) inside the card
        name = ""
        for tag in ["h3", "h2", "h4"]:
            el = li.find(tag)
            if el:
                name = el.get_text(strip=True)[:150]
                break
        if not name:
            name = a.get_text(strip=True)[:150]
        if not name:
            continue

        # Price — look for "Rs." text
        price = ""
        for el in li.find_all(string=True):
            if "rs." in el.lower() or "₹" in el:
                price = el.strip()[:20]
                break

        # Stock status — confirmed span class
        stock_el = li.find("span", class_="add-to-cart-text__content")
        if stock_el:
            stock_text = stock_el.get_text(strip=True).lower()
            in_stock = "sold out" not in stock_text
        else:
            # Fallback: scan card text
            card_text = li.get_text(separator=" ").lower()
            in_stock = "sold out" not in card_text

        # Use variant path as part of fingerprint so variants are distinct
        variant_path = href.split("?")[0] if href else ""
        fid = fingerprint(name, price, variant_path)

        items.append({
            "fid":      fid,
            "name":     name,
            "url":      full_url,
            "price":    price,
            "in_stock": in_stock,
        })

    return items

def check_diecastsilkroad(state: dict, token: str, chat_id: str) -> dict:
    log.info("[DiecastSilkRoad] Starting paginated scrape (newest first)...")
    prev: dict = state.get("diecastsilkroad", {})
    new_state: dict = dict(prev)  # start with everything we've seen before

    page = 1
    max_pages = 20  # safety cap — 365 items ÷ ~24 per page ≈ 16 pages

    while page <= max_pages:
        url = f"{DSR_LISTING}?{DSR_PARAMS}&page={page}"
        log.info(f"  Page {page}: {url}")
        html = fetch(url)

        if html is None:
            log.warning(f"  Fetch failed on page {page} — stopping.")
            break

        items = dsr_parse_page(html)
        log.info(f"  Found {len(items)} item(s) on page {page}.")

        if not items:
            log.info(f"  Empty page — done paginating.")
            break

        all_seen_before = True  # will flip if any new fingerprint found

        for item in items:
            fid = item["fid"]
            prev_item = prev.get(fid)

            # Always update state with latest snapshot
            new_state[fid] = {
                "name":     item["name"],
                "url":      item["url"],
                "price":    item["price"],
                "in_stock": item["in_stock"],
            }

            if item["in_stock"]:
                if prev_item is None:
                    # Never seen before + in stock = new stock added
                    all_seen_before = False
                    log.info(f"  NEW IN STOCK: {item['name']} ({item['price']})")
                    alert(token, chat_id, "🛒", "New stock added!",
                          "Diecast Silk Road", item["name"], item["url"])

                elif not prev_item.get("in_stock", False):
                    # Previously OOS, now back in stock
                    all_seen_before = False
                    log.info(f"  RESTOCKED: {item['name']} ({item['price']})")
                    alert(token, chat_id, "🔄", "Back in stock!",
                          "Diecast Silk Road", item["name"], item["url"])

                else:
                    log.info(f"  Unchanged (in stock): {item['name']}")

            else:
                if prev_item is None:
                    # New item but already sold out — record it, no alert
                    all_seen_before = False
                    log.info(f"  New item (already sold out): {item['name']}")
                else:
                    log.info(f"  Unchanged (sold out): {item['name']}")

        # Early exit: if every item on this page was already in state,
        # all older pages will be too — no need to keep paginating
        if all_seen_before and page > 1:
            log.info(f"  All items on page {page} already known — stopping early.")
            break

        page += 1
        jitter()

    state["diecastsilkroad"] = new_state
    return state


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        raise SystemExit(1)

    state = load_state()

    log.info("=" * 60)
    log.info("SITE 1 — karzanddolls.com")
    log.info("=" * 60)
    state = check_karzanddolls(state, token, chat_id)

    log.info("=" * 60)
    log.info("SITE 2 — diecastsilkroad.com")
    log.info("=" * 60)
    state = check_diecastsilkroad(state, token, chat_id)

    save_state(state)


if __name__ == "__main__":
    main()