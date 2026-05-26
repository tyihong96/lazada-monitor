"""
Lazada Pokemon Center SG restock monitor.

Strategy: load page, wait for it to be reasonably settled, then check
the FULL rendered body text for stock markers. We don't require any
specific selector to exist — the markers are what we care about.

This is more forgiving than the previous selector-based approach,
which broke when Lazada's PDP layout changed.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright
import requests

PRODUCTS = [
    {
        "name": "Chaos Rising ETB",
        "url": (
            "https://www.lazada.sg/products/"
            "pokemon-trading-card-game-mega-evolution-chaos-rising-"
            "pokemon-center-elite-trainer-box-limit-2-per-person-"
            "i13718947810-s124660484148.html"
        ),
    },
    {
        "name": "Ascended Heroes ETB",
        "url": (
            "https://www.lazada.sg/products/"
            "pokemon-trading-card-game-mega-evolution-ascended-heroes-"
            "pokemon-center-elite-trainer-box-limit-2-per-person-"
            "i3653880220-s24152360783.html"
        ),
    },
]

STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()  # strip trailing whitespace


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_telegram(message):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(api, json=payload, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"telegram send failed: {e}", file=sys.stderr)


async def block_heavy_resources(route):
    rtype = route.request.resource_type
    if rtype in ("image", "font", "stylesheet", "media"):
        await route.abort()
    else:
        await route.continue_()


async def dump_debug(page, name, suffix=""):
    DEBUG_DIR.mkdir(exist_ok=True)
    slug = name.lower().replace(" ", "_") + suffix
    try:
        html = await page.content()
        (DEBUG_DIR / f"{slug}.html").write_text(html[:500_000])
    except Exception:
        pass
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=False)
    except Exception:
        pass


async def check_stock(page, url, name):
    """
    Returns (in_stock: bool, reason: str).

    Approach:
    1. Load the page with a 20s timeout.
    2. Wait briefly for the buy-box area to render. If our preferred
       selectors don't exist, fall back to a small fixed delay and
       proceed anyway.
    3. Pull the FULL body text and look for stock markers.
    4. Special case: explicit "no longer available" page → delisted.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        await dump_debug(page, name, "_goto_fail")
        return False, f"goto_error: {type(e).__name__}"

    # Try the buy-box selectors with a short timeout. If they don't
    # exist (Lazada PDP layout varies), just wait a bit and continue.
    try:
        await page.wait_for_selector(
            '[data-spm="buybox"], .pdp-block--buy-now, #module_add_to_cart, '
            '.pdp-product-price, .pdp-cart-concern, [class*="buyBoxBtn"], '
            '[class*="addToCart"]',
            timeout=5000,
        )
    except Exception:
        # Selector miss is fine — Lazada may have changed class names.
        # Give the page a little more time to settle, then proceed.
        await page.wait_for_timeout(2500)

    try:
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception as e:
        return False, f"eval_error: {e}"

    if not body_text:
        await dump_debug(page, name, "_empty_body")
        return False, "empty_body"

    text_lower = body_text.lower()

    # Delisted check first — overrides everything else
    delisted_markers = [
        "this product is no longer available",
        "product is no longer available",
        "sorry! this product",
    ]
    if any(m in text_lower for m in delisted_markers):
        return False, "delisted"

    out_markers = [
        "out of stock",
        "sold out",
        "notify me when available",
        "currently unavailable",
    ]
    in_markers = [
        "add to cart",
        "buy now",
        "add to wishlist",  # WEAK marker — usually accompanies add to cart
    ]

    has_out = any(m in text_lower for m in out_markers)
    has_in_strong = any(m in text_lower for m in in_markers[:2])  # add to cart / buy now
    has_in_weak = "add to wishlist" in text_lower

    # If both an OOS marker AND a strong in-stock marker appear, trust
    # the OOS marker — Lazada often renders "Add to Cart" text in JS
    # bundles or hidden elements even when sold out.
    if has_out:
        return False, "out_of_stock"
    if has_in_strong:
        return True, "in_stock"
    if has_in_weak:
        # Only "Add to Wishlist" with no other signal: could be either.
        # Dump for inspection and treat as ambiguous (out of stock by default).
        await dump_debug(page, name, "_ambiguous")
        return False, f"ambiguous (sample={body_text[:300]!r})"

    # No marker found at all — page structure may have changed.
    await dump_debug(page, name, "_no_marker")
    return False, f"no_marker (sample={body_text[:300]!r})"


async def main():
    state = load_state()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
                "Mobile/15E148 Safari/604.1"
            ),
            viewport={"width": 390, "height": 844},
            locale="en-SG",
            extra_http_headers={
                "Accept-Language": "en-SG,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        await context.route("**/*", block_heavy_resources)
        page = await context.new_page()

        for product in PRODUCTS:
            name = product["name"]
            url = product["url"]
            in_stock, reason = await check_stock(page, url, name)
            prev = state.get(name, {}).get("in_stock", False)

            print(f"{name}: in_stock={in_stock} reason={reason} prev={prev}")

            if in_stock and not prev:
                send_telegram(
                    f"🚨 <b>RESTOCK</b> 🚨\n\n"
                    f"<b>{name}</b> is available!\n\n"
                    f"👉 {url}\n\n"
                    f"Open Lazada app → add to cart → checkout fast."
                )
            elif not in_stock and prev:
                send_telegram(f"⚪ {name} back to out of stock.")

            state[name] = {"in_stock": in_stock, "reason": reason}

        await browser.close()

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
