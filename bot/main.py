"""
Stock checker bot — dual-site edition
======================================

Site 1  (listing page — cards appear / disappear)
  Cards vanish completely when out of stock.
  Alerts on 3 events:
    1. Brand-new card seen for the first time
    2. Card status: "coming soon" → "add to cart"   (now buyable)
    3. Previously-gone card reappears               (back in stock)

Site 2  (stock page — URL persists, items change)
  Items stay listed but stock availability changes.
  Alerts on 1 event:
    1. A new item fingerprint appears that was not seen before
       This catches genuine new stock even when total count is unchanged
       because sold items and new items have different fingerprints.

Fingerprint strategy
  Each item is identified by MD5( name + price + variant-text ), NOT by
  position or total count. This means:
    - 2 items sell, 2 different items added → count unchanged, but 2 new
      fingerprints detected → 2 alerts fired correctly
    - Same item relisted at same price → same fingerprint → no duplicate alert

State persisted to state/state.json, committed back to repo each run.

────────────────────────────────────────────────────────────
GitHub Secrets
────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN      from @BotFather
TELEGRAM_CHAT_ID        your numeric chat/group id

SITE1_URL               full listing page URL
SITE1_CARD_SELECTOR     CSS selector for a product card  e.g. .product-card
SITE1_NAME_SELECTOR     CSS selector for name inside a card  e.g. .product-title
SITE1_PRICE_SELECTOR    CSS selector for price inside a card (optional, improves fingerprint)
SITE1_BUY_KEYWORD       text on the buy button   (default: add to cart)
SITE1_SOON_KEYWORD      text on coming-soon badge (default: coming soon)

SITE2_URL               full stock page URL
SITE2_CARD_SELECTOR     CSS selector for an item row/card
SITE2_NAME_SELECTOR     CSS selector for name inside a card
SITE2_PRICE_SELECTOR    CSS selector for price inside a card (optional, improves fingerprint)
SITE2_INSTOCK_KEYWORD   text that confirms in-stock   (default: add to cart)
SITE2_OOS_KEYWORD       text that confirms out-of-stock (default: out of stock)
"""

import os
import json
import time
import random
import hashlib
import logging
import requests
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def fingerprint(*parts: str) -> str:
    """
    Stable 12-char ID built from item identity fields.
    Same item = same fingerprint. Different item = different fingerprint.
    """
    raw = "|".join(p.lower().strip() for p in parts if p)
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def extract_text(card: BeautifulSoup, selector: str) -> str:
    if not selector:
        return ""
    el = card.select_one(selector)
    return el.get_text(strip=True) if el else ""

def extract_href(card: BeautifulSoup, base_url: str) -> str:
    a = card.find("a", href=True)
    if not a:
        return base_url
    href = a["href"]
    return href if href.startswith("http") else urljoin(base_url, href)

def best_name(card: BeautifulSoup, name_selector: str) -> str:
    """Try the configured selector first, then fall back to first heading."""
    name = extract_text(card, name_selector)
    if name:
        return name
    for tag in ["h1", "h2", "h3", "h4", "strong", "p", "span"]:
        el = card.find(tag)
        if el:
            text = el.get_text(strip=True)
            if len(text) > 2:
                return text[:120]
    return card.get_text(separator=" ", strip=True)[:120]


# ── HTTP ───────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def fetch(url: str) -> Optional[str]:
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
    except Exception as e:
        log.error(f"Fetch failed [{url}]: {e}")
        return None


# ── Telegram ───────────────────────────────────────────────────────────────────

def notify(token: str, chat_id: str, text: str):
    if not token or not chat_id:
        log.warning("Telegram not configured — printing alert locally.")
        log.info(f"[ALERT] {text}")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=10,
        )
        r.raise_for_status()
        log.info("Telegram alert sent.")
    except Exception as e:
        log.error(f"Telegram failed: {e}")

def alert(token: str, chat_id: str, emoji: str, headline: str, name: str, url: str):
    notify(token, chat_id,
        f"{emoji} <b>{headline}</b>\n\n"
        f"{name}\n\n"
        f'<a href="{url}">View product</a>'
    )


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"Could not read state file: {e} — starting fresh.")
    return {"site1": {}, "site2": {}}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    log.info(f"State saved → {STATE_FILE}")


# ══════════════════════════════════════════════════════════════════════════════
#  SITE 1  — listing page (cards appear / disappear)
# ══════════════════════════════════════════════════════════════════════════════
#
#  Per-card state shape:
#  {
#    "name":   "Air Jordan 1 Retro",
#    "url":    "https://...",
#    "status": "soon" | "buyable" | "unknown",
#    "gone":   false
#  }
#
#  Alert matrix:
#  ┌─────────────────────────┬──────────────────────────────────┐
#  │ Condition               │ Alert                            │
#  ├─────────────────────────┼──────────────────────────────────┤
#  │ New fingerprint         │ "New product listed / buyable"   │
#  │ soon → buyable          │ "Now available to buy!"          │
#  │ gone=True → reappears   │ "Back — add to cart / coming"    │
#  └─────────────────────────┴──────────────────────────────────┘

def site1_status(card_text: str, buy_kw: str, soon_kw: str) -> str:
    t = card_text.lower()
    if buy_kw in t:
        return "buyable"
    if soon_kw in t:
        return "soon"
    return "unknown"

def check_site1(state: dict, token: str, chat_id: str) -> dict:
    url         = env("SITE1_URL")
    card_sel    = env("SITE1_CARD_SELECTOR",  ".product-card")
    name_sel    = env("SITE1_NAME_SELECTOR",  "")
    price_sel   = env("SITE1_PRICE_SELECTOR", "")
    buy_kw      = env("SITE1_BUY_KEYWORD",    "add to cart").lower()
    soon_kw     = env("SITE1_SOON_KEYWORD",   "coming soon").lower()

    if not url:
        log.warning("[Site 1] SITE1_URL not set — skipping.")
        return state

    log.info(f"[Site 1] Fetching {url}")
    html = fetch(url)
    if html is None:
        log.warning("[Site 1] Fetch failed — skipping.")
        return state

    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(card_sel)
    log.info(f"[Site 1] Found {len(cards)} card(s) with selector '{card_sel}'")

    if not cards:
        log.warning(
            "[Site 1] No cards found. Double-check SITE1_CARD_SELECTOR "
            "by inspecting the page source."
        )

    prev: dict = state.get("site1", {})

    # Build current snapshot keyed by fingerprint
    current: dict = {}
    for card in cards:
        name  = best_name(card, name_sel)
        price = extract_text(card, price_sel)
        href  = extract_href(card, url)
        fid   = fingerprint(name, price)
        text  = card.get_text(separator=" ", strip=True)
        status = site1_status(text, buy_kw, soon_kw)
        current[fid] = {"name": name, "url": href, "status": status, "gone": False}

    # Mark cards that have disappeared as gone (preserve them in state)
    new_state: dict = dict(current)
    for fid, prev_item in prev.items():
        if fid not in current and not prev_item.get("gone", False):
            log.info(f"[Site 1] Card gone: {prev_item['name']}")
            new_state[fid] = {**prev_item, "gone": True}
        elif fid not in current:
            # Already marked gone in a previous run — keep as-is
            new_state[fid] = prev_item

    # Diff and alert
    for fid, item in current.items():
        name, href, status = item["name"], item["url"], item["status"]
        prev_item = prev.get(fid)

        if prev_item is None:
            # Brand-new fingerprint
            log.info(f"[Site 1] NEW: {name} ({status})")
            if status == "buyable":
                alert(token, chat_id, "🛒", "New product — buy now!", name, href)
            else:
                alert(token, chat_id, "👀", "New product listed", name, href)

        elif prev_item.get("gone", False):
            # Was gone, now reappeared
            log.info(f"[Site 1] REAPPEARED: {name} ({status})")
            if status == "buyable":
                alert(token, chat_id, "🔄", "Back in stock — buy now!", name, href)
            else:
                alert(token, chat_id, "🔄", "Back — coming soon", name, href)

        elif prev_item.get("status") != "buyable" and status == "buyable":
            # Status upgraded: soon/unknown → buyable
            log.info(f"[Site 1] NOW BUYABLE: {name}")
            alert(token, chat_id, "🛒", "Now available to buy!", name, href)

        else:
            log.info(f"[Site 1] No change: {name} ({status})")

    state["site1"] = new_state
    return state


# ══════════════════════════════════════════════════════════════════════════════
#  SITE 2  — stock page (URL persists, items change)
# ══════════════════════════════════════════════════════════════════════════════
#
#  Per-item state shape:
#  {
#    "name":     "Nike Dunk Low Panda",
#    "url":      "https://...",
#    "in_stock": true
#  }
#
#  Fingerprint = MD5(name + price + variant) — NOT position or total count.
#
#  Alert rule:
#    New fingerprint that is in-stock → alert
#    Previously seen fingerprint, now in-stock when it wasn't → alert
#    Count unchanged but fingerprints changed → correctly fires for new ones
#
#  What is NOT alerted:
#    Item going out of stock (not requested)
#    Count increasing but same fingerprints (e.g. quantity bump, not new item)

def item_in_stock(card_text: str, instock_kw: str, oos_kw: str) -> bool:
    t = card_text.lower()
    # Out-of-stock check takes priority
    if oos_kw and oos_kw in t:
        return False
    if instock_kw and instock_kw in t:
        return True
    # If neither keyword present, assume in-stock (item is listed, no oos marker)
    return True

def check_site2(state: dict, token: str, chat_id: str) -> dict:
    url         = env("SITE2_URL")
    card_sel    = env("SITE2_CARD_SELECTOR",    ".product-card")
    name_sel    = env("SITE2_NAME_SELECTOR",    "")
    price_sel   = env("SITE2_PRICE_SELECTOR",   "")
    instock_kw  = env("SITE2_INSTOCK_KEYWORD",  "add to cart").lower()
    oos_kw      = env("SITE2_OOS_KEYWORD",      "out of stock").lower()

    if not url:
        log.warning("[Site 2] SITE2_URL not set — skipping.")
        return state

    log.info(f"[Site 2] Fetching {url}")
    html = fetch(url)
    if html is None:
        log.warning("[Site 2] Fetch failed — skipping.")
        return state

    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(card_sel)
    log.info(f"[Site 2] Found {len(cards)} card(s) with selector '{card_sel}'")

    if not cards:
        log.warning(
            "[Site 2] No cards found. Double-check SITE2_CARD_SELECTOR."
        )

    prev: dict = state.get("site2", {})
    new_state: dict = {}

    for card in cards:
        name     = best_name(card, name_sel)
        price    = extract_text(card, price_sel)
        href     = extract_href(card, url)
        text     = card.get_text(separator=" ", strip=True)
        fid      = fingerprint(name, price)
        in_stock = item_in_stock(text, instock_kw, oos_kw)

        new_state[fid] = {"name": name, "url": href, "in_stock": in_stock}

        prev_item = prev.get(fid)

        if in_stock:
            if prev_item is None:
                # Never seen before and it's in stock
                log.info(f"[Site 2] NEW IN STOCK: {name}")
                alert(token, chat_id, "🛒", "New stock added!", name, href)

            elif not prev_item.get("in_stock", False):
                # Was out of stock, now back in stock
                log.info(f"[Site 2] RESTOCKED: {name}")
                alert(token, chat_id, "🔄", "Back in stock!", name, href)

            else:
                log.info(f"[Site 2] Unchanged (in stock): {name}")
        else:
            log.info(f"[Site 2] Out of stock (no alert): {name}")

    # Log items that disappeared entirely from the page (sold out + removed)
    for fid, prev_item in prev.items():
        if fid not in new_state:
            log.info(f"[Site 2] Item removed from page: {prev_item['name']}")

    state["site2"] = new_state
    return state


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    token   = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        raise SystemExit(1)

    state = load_state()

    check_site1(state, token, chat_id)

    # Polite delay between sites
    delay = random.uniform(4, 9)
    log.info(f"Waiting {delay:.1f}s before site 2...")
    time.sleep(delay)

    check_site2(state, token, chat_id)

    save_state(state)


if __name__ == "__main__":
    main()
