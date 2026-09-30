# Market screener dashboard

Breakout and breakdown screener for 41 US stocks/ETFs (daily, via Yahoo) and
29 Binance crypto pairs (4H, with 1H retests). It writes a self-contained,
filterable HTML dashboard. Read-only research: it never places orders.

## Quick start (macOS)

Requires Python 3.11+ (`python3 --version`).

```bash
cd dashboard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-screener.txt   # needed for stocks only

python -m screener demo          # synthetic data, no network
python -m screener once          # live scan of stocks + crypto
open .screener/live/public/index.html
```

`python -m screener once --market crypto` scans crypto only. The first live
scan sets a baseline; confirmed breakouts need later scans, so run it on a
schedule (see `.github/workflows/screener.yml`, every 4 hours) or by hand.

State lives in `.screener/live/journal.sqlite3` (private; not committed).
Serve or share only `.screener/live/public/`.

## Configure

Edit `screener.json`: watchlist, strategy thresholds and paper-trading budget.
Nothing becomes entry-eligible until `strategy.round_trip_cost_bps` is set
(e.g. `10` for 0.10% round trip).

Optional Telegram alerts (`python -m screener once --notify`): set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your shell. Never commit them.

## Dashboard

- Green ▲ LONG / red ▼ SHORT, regime colours, red stops and green targets.
- Planned trigger, entry, stop and target on rows that have not broken out yet.
- "Checks N/8" per row; expand *Evidence* for the pass/fail/not-yet checklist.
- Track record: breakout funnel and paper results in R (win rate after 30 trades).
- Filters for symbol, market, state, regime and side.

## Layout

| Path | Purpose |
|---|---|
| `screener/engine.py` | Strategy state machine, planned levels, paper trade stepping |
| `screener/feeds.py`, `screener/yahoo.py` | Binance, Yahoo/Alpaca data and market calendar |
| `screener/runtime.py` | Scan loop, SQLite journal, Telegram alerts |
| `screener/view.py` | Dashboard HTML |
| `screener/shared.py` | Errors, hashing, safe HTTP, file output, Telegram transport |
| `tests/` | `python -m unittest discover -s tests` |

Extracted from the `market-pivot-watch` repository; the helpers in
`screener/shared.py` were copied from its `pivot_watch` package so this folder
runs on its own.
