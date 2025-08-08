# Telegram 75% Fib Alert Bot (Solana / Dexscreener) — Railway-ready

This repo is a **minimal, deployment-ready** template that implements your v2 spec:
- Solana only
- Dexscreener `priceUsd`
- Top pair selection by **24h volume**, tie-break by **24h liquidity**
- Entering events only, ±2% band around 75% fib (from USD low/high)
- **Max 2 alerts per token**, then auto-stop
- Polling every **5 minutes** (async, batched)
- Commands: `/add`, `/remove`, `/list`, `/clear`

## Quick start (local)
1. `python -m venv .venv && . .venv/bin/activate` (Windows: `.venv\Scripts\activate`)
2. `pip install -r requirements.txt`
3. Create `.env` from `.env.example` and set `TELEGRAM_BOT_TOKEN`.
4. `python main.py`

## Deploy to Railway
- **No Dockerfile needed.** Railway will detect Python via `requirements.txt` and use the `Procfile` to start a worker dyno.
- Add environment variable in Railway → Variables: `TELEGRAM_BOT_TOKEN`.
- Set the service to **worker** (no HTTP) if prompted.
- Deploy.

## Notes
- State is **in-memory**, by design, and each token self-expires after 2 alerts.
- If you want durability across restarts, set `PERSIST_JSON_PATH=./state.json` as an env var (optional).

