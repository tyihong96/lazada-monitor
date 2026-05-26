"""
Lazada Pokemon Center SG restock monitor.

Detection rule:
- In stock: BOTH "Buy Now" and "Add to Cart" buttons enabled
- Out of stock: "Item Not Available" or only "Add to Wishlist"
- Delisted: "this product is no longer available" page

Sends a daily heartbeat to Telegram so you know the bot is alive.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.async_api import async_playwright
import requests

PRODUCTS = [
    {
        "name": "Chaos Rising ETB",
        "monitor_url": (
            "https://www.lazada.sg/products/"
            "pokemon-trading-card-game-mega-evolution-chaos-rising-"
            "pokemon-center-elite-trainer-box-limit-2-per-person-"
            "i13718947810-s124660484148.html"
        ),
        "app_url": "https://s.lazada.sg/s.fmYYl?c=b",
    },
    {
        "name": "Ascended Heroes ETB",
        "monitor_url": (
            "https://www.lazada.sg/products/"
            "pokemon-trading-card-game-mega-evolution-ascended-heroes-"
            "pokemon-center-elite-trainer-box-limit-2-per-person-"
            "i3653880220-s24152360783.html"
        ),
        "app_url": "https://s.lazada.sg/s.31pGa",
    },
]

STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

SG_TZ = timezone(timedelta(hours=8))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_telegram(message):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
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
        (DEBUG_DIR / f"{slug}.html").write_text(html[:1_000_000])
    except Exception:
        pass
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=False)
    except Exception:
        pass


async def check_stock(page, url, name):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        return False, f"goto_error: {type(e).__name__}"

    try:
        await page.wait_for_selector('button, [role="button"]', timeout=8000)
    except Exception:
        await page.wait_for_timeout(2500)

    try:
        body_lower = (await page.evaluate(
            "() => (document.body ? document.body.innerText : '').toLowerCase()"
        ))
    except Exception as e:
        return False, f"eval_error: {e}"

    if "this product is no longer available" in body_lower:
        return False, "delisted"

    button_info = await page.evaluate(
        """
        () => {
          const out = {has_buy_now: false, has_add_to_cart: false,
                      has_item_unavailable: false, button_texts: []};
          const els = document.querySelectorAll('button, [role="button"]');
          for (const el of els) {
            const txt = ((el.innerText || el.textContent || '').trim()).toLowerCase();
            if (!txt) continue;
            const disabled = el.disabled
              || el.getAttribute('aria-disabled') === 'true'
              || (el.className && typeof el.className === 'string'
                  && /disabled/i.test(el.className));
            if (txt.length < 80) {
              out.button_texts.push((disabled ? '[X] ' : '[ ] ') + txt);
            }
            if (disabled) continue;
            if (txt === 'buy now' || txt.startsWith('buy now')) out.has_buy_now = true;
            if (txt === 'add to cart' || txt.startsWith('add to cart')) out.has_add_to_cart = true;
            if (txt.includes('item not available') || txt.includes('out of stock')
                || txt.includes('sold out') || txt.includes('notify me')) {
              out.has_item_unavailable = true;
            }
          }
          return out;
        }
        """
    )

    print(f"  buttons: {button_info['button_texts'][:15]}")

    in_stock = button_info["has_buy_now"] and button_info["has_add_to_cart"]

    if in_stock:
        return True, "buy_now+add_to_cart"
    if button_info["has_item_unavailable"]:
        return False, "item_not_available"
    await dump_debug(page, name, "_unknown")
    return False, "no_clear_signal"


def maybe_send_heartbeat(state, statuses):
    """
    Send a daily heartbeat at the FIRST run of each SG day (13:00 SG).
    Includes current state of both products so you can sanity-check
    that the bot is seeing what you expect.
    """
    today_sg = datetime.now(SG_TZ).strftime("%Y-%m-%d")
    last_heartbeat = state.get("_last_heartbeat")
    if last_heartbeat == today_sg:
        return  # already sent today
    lines = [f"💓 Bot alive — {today_sg} 1pm SG"]
    for name, reason in statuses:
        lines.append(f"• <b>{name}</b>: {reason}")
    send_telegram("\n".join(lines))
    state["_last_heartbeat"] = today_sg


async def main():
    state = load_state()
    statuses = []  # (name, reason) for heartbeat

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
            extra_http_headers={"Accept-Language": "en-SG,en;q=0.9"},
        )
        await context.route("**/*", block_heavy_resources)
        page = await context.new_page()

        for product in PRODUCTS:
            name = product["name"]
            in_stock, reason = await check_stock(page, product["monitor_url"], name)
            prev = state.get(name, {}).get("in_stock", False)
            print(f"{name}: in_stock={in_stock} reason={reason} prev={prev}")
            statuses.append((name, reason))

            if in_stock and not prev:
                send_telegram(
                    f"🚨 <b>RESTOCK</b> 🚨\n\n"
                    f"<b>{name}</b> is available!\n\n"
                    f"👉 Tap to open in Lazada app:\n"
                    f"{product['app_url']}\n\n"
                    f"Add to cart → checkout fast."
                )
            elif not in_stock and prev:
                send_telegram(f"⚪ {name} back to out of stock.")

            state[name] = {"in_stock": in_stock, "reason": reason}

        await browser.close()

    # Daily heartbeat — once per SG day, after the checks succeed
    maybe_send_heartbeat(state, statuses)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
