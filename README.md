# Stock Checker Bot

Monitors two product pages every 5 minutes and sends Telegram alerts.
Runs entirely free on GitHub Actions. State is committed back to the repo
after each run so changes are detected correctly across runs.

---

## How fingerprinting works

Each product is identified by `MD5(name + price + variant)` — not by position
or total count. This means:

- 2 items sell, 2 different items added → count unchanged, but 2 new
  fingerprints appear → 2 alerts fire correctly
- Same item relisted at same price → same fingerprint → no duplicate alert

---

## Setup

### 1. Push to a private GitHub repo

```bash
git init
git add .
git commit -m "init"
gh repo create stock-bot --private --push --source .
```

### 2. Create a Telegram bot

1. Open Telegram, search **@BotFather**
2. `/newbot` → follow prompts → copy the token
3. Send your bot any message to start a chat
4. Get your chat ID:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look for `"chat":{"id": 123456789}`

### 3. Find your CSS selectors

Open the product listing page in Chrome, right-click a product card →
**Inspect**. Look for a class or element that wraps each card consistently.
Common patterns:

```
.product-card
.product-item
.grid-item
article.product
li.item
```

For the name inside a card, look for the heading or link text element:
```
.product-title
.product-name
h2.title
a.product-link
```

For price (optional but improves fingerprint accuracy):
```
.price
.product-price
span[class*="price"]
```

### 4. Add GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**

#### Required for both sites
| Secret | Value |
|--------|-------|
| `TELEGRAM_BOT_TOKEN` | Your BotFather token |
| `TELEGRAM_CHAT_ID` | Your numeric chat ID |

#### Site 1 (listing page — cards disappear when out of stock)
| Secret | Value |
|--------|-------|
| `SITE1_URL` | Full listing page URL |
| `SITE1_CARD_SELECTOR` | CSS selector for a product card |
| `SITE1_NAME_SELECTOR` | CSS selector for product name inside card |
| `SITE1_PRICE_SELECTOR` | CSS selector for price inside card *(optional)* |
| `SITE1_BUY_KEYWORD` | Text on buy button (default: `add to cart`) |
| `SITE1_SOON_KEYWORD` | Text on coming-soon badge (default: `coming soon`) |

#### Site 2 (stock page — items stay visible, stock changes)
| Secret | Value |
|--------|-------|
| `SITE2_URL` | Full stock page URL |
| `SITE2_CARD_SELECTOR` | CSS selector for a product card/row |
| `SITE2_NAME_SELECTOR` | CSS selector for item name inside card |
| `SITE2_PRICE_SELECTOR` | CSS selector for price inside card *(optional)* |
| `SITE2_INSTOCK_KEYWORD` | Text confirming in stock (default: `add to cart`) |
| `SITE2_OOS_KEYWORD` | Text confirming out of stock (default: `out of stock`) |

### 5. Enable the workflow

Repo → **Actions** tab → enable workflows → run manually first to test.

---

## Alert reference

### Site 1
| Event | Alert |
|-------|-------|
| New card, status = buyable | 🛒 New product — buy now! |
| New card, status = coming soon | 👀 New product listed |
| Card: coming soon → add to cart | 🛒 Now available to buy! |
| Card was gone, reappears buyable | 🔄 Back in stock — buy now! |
| Card was gone, reappears coming soon | 🔄 Back — coming soon |

### Site 2
| Event | Alert |
|-------|-------|
| New fingerprint, in stock | 🛒 New stock added! |
| Known fingerprint, was OOS, now in stock | 🔄 Back in stock! |
| Count unchanged but fingerprints changed | Correctly fires for new items only |

---

## File structure

```
stock-bot/
├── .github/
│   └── workflows/
│       └── check.yml        ← cron schedule + state commit
├── bot/
│   └── checker.py           ← scraper + diff engine + notifier
├── state/
│   └── state.json           ← persisted item fingerprints (auto-updated)
├── requirements.txt
└── README.md
```

---

## Troubleshooting

**No cards found** — Run this in your terminal to check what the page
actually returns without JS:
```bash
curl -s "YOUR_URL" | grep -i "your-product-name"
```
If nothing shows, the site may load products via JavaScript — contact me
to add Playwright support.

**Getting alerts for items already in stock** — This happens on the very
first run because state.json is empty. After one clean run the baseline
is set and only new changes will alert.

**Duplicate alerts** — Make sure `SITE1_PRICE_SELECTOR` / `SITE2_PRICE_SELECTOR`
are set if multiple items share the same name (e.g. same shoe, different sizes).
Including price in the fingerprint differentiates them.
