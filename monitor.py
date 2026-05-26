"""
Lazada Pokemon Center SG restock monitor.

Diagnostic version: on load_error, dumps the page HTML and a screenshot
as workflow artifacts so we can see what Lazada actually served.
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
    rtype = route.request.resource_type
    if rtype in ("image", "font", "stylesheet", "media"):
        await route.abort()
    else:
        await route.continue_()


async def dump_debug(page, name):
    """Save HTML + screenshot for failed loads so we can see what happened."""
    DEBUG_DIR.mkdir(exist_ok=True)
    slug = name.lower().replace(" ", "_")
    try:
        html = await page.content()
        (DEBUG_DIR / f"{slug}.html").write_text(html[:500_000])
    except Exception:
        pass
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=False)
    except Exception:
        pass
    # Also log a snippet of the page title and visible text to stdout
    # so we can see something useful in the logs without downloading artifacts.
    try:
        title = await page.title()
        body_sample = await page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 500) : '(no body)'"
        )
        print(f"  DEBUG title: {title!r}")
        print(f"  DEBUG body sample: {body_sample!r}")
    except Exception as e:
        print(f"  DEBUG dump failed: {e}")


async def check_stock(page, url, name):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        await dump_debug(page, name)
        return False, f"goto_error: {type(e).__name__}"

    # Try to find the buy-box. If it doesn't appear, dump debug info.
    try:
        await page.wait_for_selector(
            '[data-spm="buybox"], .pdp-block--buy-now, #module_add_to_cart, .pdp-product-price',
            timeout=10000,
        )
    except Exception as e:
        await dump_debug(page, name)
        return False, f"selector_error: {type(e).__name__}"

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
            # Extra headers to look more like a real mobile browser
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
