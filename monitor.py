"""
Lazada Pokemon Center SG restock monitor — optimized for fast runs.

Runs on GitHub Actions every 1 min during SG 1:00-2:00pm window.
Uses Playwright with aggressive timeouts and minimal waits to keep
each run under ~45 sec, fitting GitHub free tier at 1-min cadence.

Optimizations vs initial version:
- Block images/fonts/css (Lazada loads ~5MB of these per page)
- Single explicit wait on the buy-box selector, no extra sleep
- Bail fast on load errors instead of retrying
- Reuse browser context across both products in same run
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
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


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
    """Block images, fonts, stylesheets, media — none affect stock state."""
    rtype = route.request.resource_type
    if rtype in ("image", "font", "stylesheet", "media"):
        await route.abort()
    else:
        await route.continue_()


async def check_stock(page, url):
    """Return (in_stock: bool, reason: str). Fast path, no retries."""
    try:
        # domcontentloaded > networkidle: we want the JS to start, not finish
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        # Single wait on the buy-box. If it doesn't appear in 8s, bail.
        await page.wait_for_selector(
            '[data-spm="buybox"], .pdp-block--buy-now, #module_add_to_cart',
            timeout=8000,
        )
    except Exception as e:
        return False, f"load_error: {type(e).__name__}"

    try:
        body_text = await page.evaluate(
            """
            () => {
              const candidates = [
                document.querySelector('[data-spm="buybox"]'),
                document.querySelector('.pdp-block--buy-now'),
                document.querySelector('.pdp-cart-concern'),
                document.querySelector('#module_add_to_cart'),
              ].filter(Boolean);
              if (candidates.length === 0) return document.body.innerText;
              return candidates.map(el => el.innerText).join('\\n');
            }
            """
        )
    except Exception as e:
        return False, f"eval_error: {e}"

    text_lower = body_text.lower()
    out_markers = ["out of stock", "sold out", "notify me when available", "notify me"]
    in_markers = ["add to cart", "buy now"]

    has_out = any(m in text_lower for m in out_markers)
    has_in = any(m in text_lower for m in in_markers)

    if has_out:
        return False, "out_of_stock"
    if has_in:
        return True, "add_to_cart_visible"
    return False, f"no_marker (sample={body_text[:200]!r})"


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
        )
        # Block heavy resources globally for this context.
        await context.route("**/*", block_heavy_resources)

        page = await context.new_page()

        for product in PRODUCTS:
            name = product["name"]
            url = product["url"]
            in_stock, reason = await check_stock(page, url)
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
