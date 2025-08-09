import asyncio
import os
import json
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
STATE_PATH = os.environ.get("STATE_PATH", "/app/watchlist.json")  # where we save/load watchlist

HEADERS = {"User-Agent": "fib75-telegram-bot/1.5"}
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

# ---------- persistence helpers ----------
def _state_to_jsonable(state: Dict[int, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """Convert Decimals/tuples to JSON-friendly forms."""
    out: Dict[str, Any] = {}
    for chat_id, contracts in state.items():
        chat_key = str(chat_id)
        out[chat_key] = {}
        for contract, st in contracts.items():
            out_st = dict(st)
            # convert Decimals to strings
            for key in ["L", "H", "fib75"]:
                if key in out_st and isinstance(out_st[key], Decimal):
                    out_st[key] = str(out_st[key])
            if "band" in out_st:
                lo, hi = out_st["band"]
                out_st["band"] = [str(lo), str(hi)]
            if "last_price" in out_st and isinstance(out_st["last_price"], Decimal):
                out_st["last_price"] = str(out_st["last_price"])
            out[chat_key][contract] = out_st
    return out

def _jsonable_to_state(data: Dict[str, Any]) -> Dict[int, Dict[str, Dict[str, Any]]]:
    """Convert strings back to Decimals/tuples."""
    restored: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for chat_key, contracts in (data or {}).items():
        try:
            chat_id = int(chat_key)
        except Exception:
            continue
        restored[chat_id] = {}
        for contract, st in contracts.items():
            st2 = dict(st)
            for key in ["L", "H", "fib75"]:
                if key in st2 and isinstance(st2[key], str):
                    try: st2[key] = Decimal(st2[key])
                    except Exception: st2[key] = Decimal("0")
            if "band" in st2 and isinstance(st2["band"], list) and len(st2["band"]) == 2:
                try:
                    st2["band"] = (Decimal(st2["band"][0]), Decimal(st2["band"][1]))
                except Exception:
                    st2["band"] = (Decimal("0"), Decimal("0"))
            if "last_price" in st2 and isinstance(st2["last_price"], str):
                try: st2["last_price"] = Decimal(st2["last_price"])
                except Exception: st2["last_price"] = None
            # sanity defaults
            st2.setdefault("status", "outside")
            st2.setdefault("first_tick", True)
            st2.setdefault("alerts_sent", 0)
            st2.setdefault("pair", {})
            st2.setdefault("name", st2.get("name") or "")
            restored[chat_id][contract] = st2
    return restored

def save_state():
    try:
        data = _state_to_jsonable(chat_state)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, STATE_PATH)  # atomic on POSIX
        print(f"[STATE] saved to {STATE_PATH}")
    except Exception as e:
        print(f"[STATE] save error: {e}")

def load_state():
    global chat_state
    try:
        if not os.path.exists(STATE_PATH):
            print(f"[STATE] no existing state at {STATE_PATH}")
            return
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        chat_state = _jsonable_to_state(data)
        print(f"[STATE] loaded from {STATE_PATH}")
    except Exception as e:
        print(f"[STATE] load error: {e}")

# --------- API helpers ----------
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
        "💾 v1.5 — persistence ON (auto-saves watchlist).\n\n"
        "Commands:\n"
        "/add <contract> <low_usd> <high_usd> [name]\n"
        "/remove <contract>\n"
        "/list\n"
        "/clear\n"
        "/version\n"
        "/ping\n"
        f"(State file: {STATE_PATH})"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start(update, context)

async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[CMD] /version from chat={update.effective_chat.id}")
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "no-commit")
    await update.message.reply_text(f"fib75-bot version 1.5 (persistence) | commit: {sha}")

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
        "pair": {},
        "last_price": None
    }

    save_state()
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
    save_state()
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
        name = st.get("name") or contract
        lo, hi = st["band"]

        # Try to use the saved Dexscreener URL; if missing, fetch once.
        pair_url = (st.get("pair") or {}).get("url")
        if not pair_url:
            p = await fetch_top_pair(contract)
            if p:
                pair_url = build_pair_url(p)
                st["pair"] = {
                    "url": pair_url,
                    "dex": p.get("dexId"),
                    "base": (p.get("baseToken") or {}).get("symbol") or (p.get("baseToken") or {}).get("name") or "Token",
                    "quote": (p.get("quoteToken") or {}).get("symbol") or (p.get("quoteToken") or {}).get("name") or "",
                }

        entry = (
            f"{name}\n"
            f"  Fib75: {fmt_usd(st['fib75'])} | Band: [{fmt_usd(lo)} — {fmt_usd(hi)}] USD\n"
            f"  Alerts: {st['alerts_sent']}/2 | Last pair: {pair_url if pair_url else 'N/A'}"
        )
        lines.append(entry)

    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)
    chat_state[chat_id].clear()
    save_state()
    await update.message.reply_text("Cleared all tracked contracts for this chat.")

# -------- background price loop --------
async def poll_job(context_like):
    changed = False
    for chat_id, contracts in list(chat_state.items()):
        to_remove = []
        for contract, st in list(contracts.items()):
            if st["alerts_sent"] >= 2:
                to_remove.append(contract); changed = True; continue
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
                changed = True
            lo, hi = st["band"]
            inside = within_band(price, lo, hi)
            if inside and (st["status"] == "outside" or st["first_tick"]):
                st["alerts_sent"] += 1; changed = True
                st["status"] = "inside"; st["first_tick"] = False
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
                new_status = "inside" if inside else "outside"
                if new_status != st["status"]:
                    st["status"] = new_status; changed = True
            st["last_price"] = price
        for c in to_remove:
            contracts.pop(c, None)
    if changed:
        save_state()

async def poll_loop(application):
    # Ensure polling works even if a webhook was set earlier
    try:
        await application.bot.delete_webhook(drop_pending_updates=False)
        print("[INIT] delete_webhook ok")
    except Exception as e:
        print(f"[WARN] delete_webhook: {e}")
    while True:
        class Ctx:
            bot = application.bot
        try:
            await poll_job(Ctx)
        except Exception as e:
            print(f"[WARN] poll_job: {e}")
        await asyncio.sleep(POLL_SECONDS)

def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN env var")
    load_state()  # load from disk on startup

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
