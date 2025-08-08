
import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Dict, Optional, Tuple

import httpx
from pydantic import BaseModel, Field, ValidationError
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("fib75_bot")

# ---------- Decimal precision ----------
getcontext().prec = 28  # generous precision to avoid float drift

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))  # 5 minutes default
PERSIST_JSON_PATH = os.environ.get("PERSIST_JSON_PATH", "")  # optional persistence

DEX_API = "https://api.dexscreener.com/latest/dex/tokens/{contract}"

# ---------- Data models ----------
class Pair(BaseModel):
    chainId: str = Field(default="")
    url: Optional[str] = None
    priceUsd: Optional[str] = None
    # nested fields can be absent; use dict access with .get
    liquidity: Optional[dict] = None
    volume: Optional[dict] = None
    # Sometimes 'updatedAt' or 'pairCreatedAt' exists; we won't rely on them strictly

class TokenState(BaseModel):
    contract: str
    low_usd: Decimal
    high_usd: Decimal
    fib75_usd: Decimal
    band_min: Decimal
    band_max: Decimal
    alerts_sent: int = 0
    prev_position: str = "unknown"   # "below" | "inside" | "above" | "unknown"
    token_name: str = ""
    pair_url: str = ""

# ---------- Global watchlist in memory ----------
WATCHLIST: Dict[str, TokenState] = {}

# ---------- Helpers ----------
def dquant(x: Decimal, places=6) -> Decimal:
    q = Decimal("1." + "0"*places)
    return x.quantize(q, rounding=ROUND_HALF_UP)

def parse_decimal(s: str) -> Optional[Decimal]:
    try:
        return Decimal(str(s))
    except Exception:
        return None

def pick_top_pair(pairs: list) -> Optional[Pair]:
    # Filter to Solana pairs only
    sol_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
    if not sol_pairs:
        return None

    def vol_h24(p):
        v = (p.get("volume") or {}).get("h24")
        try:
            return float(v)
        except Exception:
            return -1.0

    def liq_usd(p):
        l = (p.get("liquidity") or {}).get("usd")
        try:
            return float(l)
        except Exception:
            return -1.0

    sol_pairs.sort(key=lambda p: (vol_h24(p), liq_usd(p)), reverse=True)
    try:
        return Pair.model_validate(sol_pairs[0])
    except ValidationError:
        return None

async def fetch_best_pair(contract: str) -> Optional[Pair]:
    url = DEX_API.format(contract=contract)
    timeout = httpx.Timeout(10, connect=5)
    async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json"}) as client:
        r = await client.get(url)
        if r.status_code != 200:
            log.warning("Dexscreener %s -> %s", url, r.status_code)
            return None
        data = r.json()
        pairs = data.get("pairs") or []
        if not isinstance(pairs, list):
            return None
        return pick_top_pair(pairs)

def position(price: Decimal, band_min: Decimal, band_max: Decimal) -> str:
    if price < band_min:
        return "below"
    if price > band_max:
        return "above"
    return "inside"

def compute_fib_band(low: Decimal, high: Decimal) -> Tuple[Decimal, Decimal, Decimal]:
    # Fib75 (25% above the low) = L + 0.25 * (H - L)
    fib = low + (Decimal("0.25") * (high - low))
    band_min = fib * Decimal("0.98")
    band_max = fib * Decimal("1.02")
    return (fib, band_min, band_max)

def persist_state():
    if not PERSIST_JSON_PATH:
        return
    try:
        payload = {k: v.model_dump() for k, v in WATCHLIST.items()}
        with open(PERSIST_JSON_PATH, "w") as f:
            json.dump(payload, f, default=str, indent=2)
    except Exception as e:
        log.warning("Persist error: %s", e)

def load_state():
    if not PERSIST_JSON_PATH:
        return
    try:
        if os.path.exists(PERSIST_JSON_PATH):
            with open(PERSIST_JSON_PATH) as f:
                payload = json.load(f)
            for k, v in payload.items():
                # restore decimals
                for fld in ("low_usd","high_usd","fib75_usd","band_min","band_max"):
                    v[fld] = Decimal(v[fld])
                WATCHLIST[k] = TokenState(**v)
            log.info("Restored %d tokens from %s", len(WATCHLIST), PERSIST_JSON_PATH)
    except Exception as e:
        log.warning("Load error: %s", e)

# ---------- Command handlers ----------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 3:
        await update.message.reply_text("Usage: /add <contract> <low_usd> <high_usd>")
        return
    contract, low_s, high_s = context.args
    low = parse_decimal(low_s)
    high = parse_decimal(high_s)
    if low is None or high is None:
        await update.message.reply_text("Invalid number. Use plain decimals for low/high.")
        return
    if not (Decimal("0") <= low < high):
        await update.message.reply_text("Constraint: 0 <= low < high")
        return

    fib, bmin, bmax = compute_fib_band(low, high)

    # Fetch a pair once here (optional) for name/url preview
    pair = await fetch_best_pair(contract)
    token_name = ""
    pair_url = ""
    if pair:
        token_name = (pair.url or "").split("/")[-1] if pair.url else ""
        pair_url = pair.url or ""

    WATCHLIST[contract] = TokenState(
        contract=contract,
        low_usd=low,
        high_usd=high,
        fib75_usd=fib,
        band_min=bmin,
        band_max=bmax,
        alerts_sent=0,
        prev_position="unknown",
        token_name=token_name,
        pair_url=pair_url
    )
    persist_state()
    await update.message.reply_text(
        f"Added {contract}\n"
        f"Fib75: {dquant(fib)} USD\n"
        f"Band: [{dquant(bmin)} – {dquant(bmax)}] USD\n"
        f"Alerts: {0}/2"
    )

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /remove <contract>")
        return
    contract = context.args[0]
    if contract in WATCHLIST:
        WATCHLIST.pop(contract, None)
        persist_state()
        await update.message.reply_text(f"Removed {contract}")
    else:
        await update.message.reply_text("Not tracking that contract.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WATCHLIST:
        await update.message.reply_text("No tokens are being tracked.")
        return
    lines = []
    for t in WATCHLIST.values():
        lines.append(
            f"{t.contract}\n"
            f"  Fib75: {dquant(t.fib75_usd)} USD | Band: [{dquant(t.band_min)} – {dquant(t.band_max)}] USD\n"
            f"  Alerts: {t.alerts_sent}/2 | Last pair: {t.pair_url or 'n/a'}"
        )
    await update.message.reply_text("\n\n".join(lines))

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    WATCHLIST.clear()
    persist_state()
    await update.message.reply_text("Cleared all tracked tokens.")

# ---------- Alert loop ----------
async def alert_loop(app):
    await asyncio.sleep(2)  # small delay after startup
    log.info("Alert loop started; interval=%ss", POLL_SECONDS)
    while True:
        if WATCHLIST:
            # Copy keys to avoid RuntimeError on mutation during iteration
            contracts = list(WATCHLIST.keys())
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
                for contract in contracts:
                    state = WATCHLIST.get(contract)
                    if not state:
                        continue
                    # Auto-stop if already 2 alerts (paranoia)
                    if state.alerts_sent >= 2:
                        WATCHLIST.pop(contract, None)
                        persist_state()
                        continue

                    # Get best pair
                    try:
                        resp = await client.get(DEX_API.format(contract=contract), headers={"Accept": "application/json"})
                        if resp.status_code != 200:
                            log.warning("Dexscreener fetch %s -> %s", contract, resp.status_code)
                            continue
                        data = resp.json()
                        pairs = data.get("pairs") or []
                        best = pick_top_pair(pairs)
                        if not best:
                            log.info("No valid Solana pair for %s", contract)
                            continue

                        state.token_name = state.token_name or (best.url or "").split("/")[-1]
                        state.pair_url = best.url or state.pair_url

                        price = None
                        if best.priceUsd is not None:
                            price = parse_decimal(best.priceUsd)

                        if price is None:
                            # skip cycle if no price
                            continue

                        pos_now = position(price, state.band_min, state.band_max)

                        if state.prev_position in ("below", "above") and pos_now == "inside" and state.alerts_sent < 2:
                            # Fire alert
                            text = (
                                "🚨 75% Fib Retracement Alert! 🚨\n"
                                f"Token: {state.token_name or contract}\n"
                                f"Level Hit: {dquant(state.fib75_usd)} USD\n"
                                f"Range: [{dquant(state.band_min)} – {dquant(state.band_max)}] USD\n"
                                f"Price: {dquant(price)} USD\n"
                                f"Dexscreener: {state.pair_url or 'n/a'}"
                            )
                            # Broadcast to all chats is out-of-scope; reply to last chat isn't reliable.
                            # Minimal approach: we store last chat_id seen in this process (single-user bot typical).
                            chat_id = app.bot_data.get("last_chat_id")
                            if chat_id:
                                await app.bot.send_message(chat_id=chat_id, text=text)
                            state.alerts_sent += 1
                            if state.alerts_sent >= 2:
                                # auto stop
                                WATCHLIST.pop(contract, None)
                            persist_state()

                        state.prev_position = pos_now

                    except Exception as e:
                        log.exception("Loop error for %s: %s", contract, e)

        await asyncio.sleep(POLL_SECONDS)

# ---------- Utilities to capture a chat_id ----------
async def remember_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # store last chat id to send alerts back
    context.application.bot_data["last_chat_id"] = update.effective_chat.id

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat_id(update, context)
    await update.message.reply_text("Bot is up. Use /add <contract> <low> <high> to begin.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_chat_id(update, context)
    await update.message.reply_text(
        "/add <contract> <low> <high>\n"
        "/remove <contract>\n"
        "/list\n"
        "/clear"
    )

# ---------- Main ----------
def main():
    load_state()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("clear", cmd_clear))

    # background task
    # app.job_queue.run_repeating(lambda ctx: None, interval=3600)  # keep JobQueue alive
    # start alert loop
    app.post_init = lambda app: asyncio.create_task(alert_loop(app))

    log.info("Starting bot...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
