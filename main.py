import asyncio
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, Any, Optional

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- config/env ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DEX_API = "https://api.dexscreener.com/latest/dex/tokens/{contract}"
POLL_SECONDS = 5 * 60  # 5 minutes

HEADERS = {"User-Agent": "fib75-telegram-bot/NEW-1.3"}
chat_state: Dict[int, Dict[str, Dict[str, Any]]] = {}  # per-chat in-memory

def d(x, q=8):
    if not isinstance(x, Decimal):
        x = Decimal(str(x))
    return x.quantize(Decimal(10) ** -q, rounding=ROUND_HALF_UP)

def compute_fib75(L: Decimal, H: Decimal) -> Decimal:
    return L + Decimal("0.25") * (H - L)

def band_bounds(fib75: Decimal):
    return (fib75 * Decimal("0.98"), fib75 * Decimal("1.02"))

def within_band(price: Decimal, lo: Decimal, hi: Decimal) -> bool:
    return lo <= price <= hi

def ensure_chat(chat_id: int):
    if chat_id not in chat_state:
        chat_state[chat_id] = {}

def fmt_usd(x: Decimal) -> str:
    if x >= Decimal("1"):
        return f"{x.quantize(Decimal('0.0001'))} USD"
    else:
        return f"{x.quantize(Decimal('0.0000001'))} USD"

def build_pair_url(pair: Dict[str, Any]) -> str:
    return pair.get("url") or "https://dexscreener.com/solana"

async def fetch_top_pair(contract: str) -> Optional[Dict[str, Any]]:
    url = DEX_API.format(contract=contract)
    try:
        async with httpx.AsyncClient(timeout=10, headers=HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        print(f"[ERR] fetch_top_pair({contract}): {e}")
        return None

    pairs = data.get("pairs") or []
    best = None
    for p in pairs:
        try:
            if p.get("chainId") != "solana":
                continue
            vol = Decimal(str(((p.get("volume") or {}).get("h24")) or 0))
            liq = Decimal(str(((p.get("liquidity") or {}).get("usd")) or 0))
            score = (vol, liq)
            if best is None or score > best[0]:
                best = (score, p)
        except Exception:
            continue
    return best[1] if best else None

async def get_price_usd_from_pair(pair: Dict[str, Any]) -> Optional[Decimal]:
    try:
        price_str = pair.get("priceUsd")
        if price_str is None:
            return None
        return Decimal(str(price_str))
    except (InvalidOperation, TypeError):
        return None

# --------- Commands ---------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[CMD] /start from chat={update.effective_chat.id}")
    await update.message.reply_text(
        "🔥 NEW BUILD v1.3 — name support ON.\n\n"
        "Commands:\n"
        "/add <contract> <low_usd> <high_usd> [name]\n"
        "/remove <contract>\n"
        "/list\n"
        "/clear\n"
        "/version\n"
        "/ping\n\n"
        "Tip: put a custom name at the end so you can recognize each watch."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start(update, context)

async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[CMD] /version from chat={update.effective_chat.id}")
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "no-commit")
    await update.message.reply_text(f"fib75-bot version 1.3 (name support) | commit: {sha}")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[CMD] /ping from chat={update.effective_chat.id}")
    await update.message.reply_text("pong")

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)

    raw = (update.message.text or "")
    parts = raw.split()
    if len(parts) < 4:
        return await update.message.reply_text("Usage: /add <contract> <low_usd> <high_usd> [name]")

    contract, Ls, Hs = parts[1], parts[2], parts[3]
    name_given = " ".join(parts[4:]).strip() if len(parts) > 4 else ""

    try:
        L = Decimal(Ls)
        H = Decimal(Hs)
        if not (L < H):
            return await update.message.reply_text("Low must be < High. Try again.")
    except InvalidOperation:
        return await update.message.reply_text("Low/High must be numbers in USD. Try again.")

    fib75 = compute_fib75(L, H)
    lo, hi = band_bounds(fib75)

    pair = await fetch_top_pair(contract)
    if not pair:
        return await update.message.reply_text("Could not find a valid Solana pair for that contract (24h volume/liquidity required).")

    base_sym = (pair.get("baseToken") or {}).get("symbol") or (pair.get("baseToken") or {}).get("name") or "Token"
    quote_sym = (pair.get("quoteToken") or {}).get("symbol") or (pair.get("quoteToken") or {}).get("name") or ""
    auto_name = f"{base_sym}/{quote_sym}" if base_sym and quote_sym else base_sym
    display_name = name_given if name_given else auto_name

    chat_state[chat_id][contract] = {
    "name": display_name,
    "L": L, "H": H,
    "fib75": fib75,
    "band": (lo, hi),
    "status": "outside",
    "first_tick": True,
    "alerts_sent": 0,
    "pair": {
        "url": build_pair_url(pair)  # store the Dexscreener link right away
    }
}


    print(f"[ADD] chat={chat_id} contract={contract} name='{display_name}' L={L} H={H} fib75={fib75}")
    await update.message.reply_text(
        f"Added *{display_name}* (`{contract}`).\n"
        f"Fib75: {fmt_usd(fib75)}\n"
        f"Band: [{fmt_usd(lo)} — {fmt_usd(hi)}]\n"
        f"Max alerts: 2 (auto-stop). Polling every 5 minutes.",
        parse_mode=ParseMode.MARKDOWN
    )

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)
    parts = (update.message.text or "").split()
    if len(parts) != 2:
        return await update.message.reply_text("Usage: /remove <contract>")
    contract = parts[1]
    existed = chat_state[chat_id].pop(contract, None)
    if existed:
        await update.message.reply_text(f"Removed {existed.get('name', contract)}.")
    else:
        await update.message.reply_text("That contract was not being tracked.")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)
    if not chat_state[chat_id]:
        return await update.message.reply_text("Nothing is being tracked.")
    lines = []
    for contract, st in chat_state[chat_id].items():
        lo, hi = st["band"]
        name = st.get("name") or contract
        lines.append(
            f"- *{name}*  (`{contract}`)\n"
            f"  Fib75: {fmt_usd(st['fib75'])} | Band: [{fmt_usd(lo)} — {fmt_usd(hi)}] | Alerts: {st['alerts_sent']}/2"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)
    chat_state[chat_id].clear()
    await update.message.reply_text("Cleared all tracked contracts for this chat.")

# -------- background price loop --------
async def poll_job(context_like):
    for chat_id, contracts in list(chat_state.items()):
        to_remove = []
        for contract, st in list(contracts.items()):
            if st["alerts_sent"] >= 2:
                to_remove.append(contract); continue
            pair = await fetch_top_pair(contract)
            if not pair:
                continue
            price = await get_price_usd_from_pair(pair)
            if price is None:
                continue
            base_sym = (pair.get("baseToken") or {}).get("symbol") or (pair.get("baseToken") or {}).get("name") or "Token"
            quote_sym = (pair.get("quoteToken") or {}).get("symbol") or (pair.get("quoteToken") or {}).get("name") or ""
            st["pair"] = {"url": build_pair_url(pair), "dex": pair.get("dexId"), "base": base_sym, "quote": quote_sym}
            if not st.get("name"):
                st["name"] = f"{base_sym}/{quote_sym}" if base_sym and quote_sym else base_sym
            lo, hi = st["band"]
            inside = within_band(price, lo, hi)
            if inside and (st["status"] == "outside" or st["first_tick"]):
                st["alerts_sent"] += 1
                st["status"] = "inside"
                st["first_tick"] = False
                if st["alerts_sent"] <= 2:
                    msg = (
                        "🚨 *75% Fib Retracement Alert!* 🚨\n"
                        f"*Watch:* {st['name']}\n"
                        f"*Token:* {st['pair']['base']}\n"
                        f"*Level Hit:* {fmt_usd(st['fib75'])}\n"
                        f"*Band:* [{fmt_usd(lo)} — {fmt_usd(hi)}]\n"
                        f"*Price Now:* {fmt_usd(price)}\n"
                        f"*Pair:* {st['pair']['dex']} / {st['pair']['quote']}\n"
                        f"[Dexscreener]({st['pair']['url']})\n"
                        f"_Alerts sent for this contract:_ {st['alerts_sent']}/2"
                    )
                    try:
                        await context_like.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
                    except Exception as e:
                        print(f"[ERR] send_message: {e}")
                if st["alerts_sent"] >= 2:
                    to_remove.append(contract)
            else:
                st["status"] = "inside" if inside else "outside"
            st["last_price"] = price
        for c in to_remove:
            contracts.pop(c, None)

async def poll_loop(application):
    # delete webhook so polling gets updates
    try:
        await application.bot.delete_webhook(drop_pending_updates=False)
        print("[INIT] delete_webhook ok")
    except Exception as e:
        print(f"[WARN] delete_webhook: {e}")
    while True:
        class Ctx:  # tiny shim to reuse poll_job
            bot = application.bot
        try:
            await poll_job(Ctx)
        except Exception as e:
            print(f"[WARN] poll_job: {e}")
        await asyncio.sleep(POLL_SECONDS)

def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN env var")
    app = (ApplicationBuilder().token(TELEGRAM_TOKEN).build())

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("version", version_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))

    # background loop + polling
    loop = asyncio.get_event_loop()
    loop.create_task(poll_loop(app))
    print("Bot starting…")
    print("Background poller started.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
