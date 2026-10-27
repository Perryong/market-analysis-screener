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

## Agent office

```bash
python -m screener office          # → http://127.0.0.1:8765 (localhost only)
```

A live view of the pipeline: Feed, Engine, Charts, Analyst and Publisher work at their desks as a
job runs, and a calendar (press `C`) shows every report run from `history/`. The RUN button offers
Demo (offline), Live scan and Report dry run only; the real report (Telegram + git push) stays on
the command line. Page: `screener/office.html`; server: `screener/office.py`.

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

## Review reporting accuracy locally

```bash
python -m screener report --preview-history history --out .screener/report-review
python -m http.server 8766 --bind 127.0.0.1 --directory .screener/report-review
```

Open http://127.0.0.1:8766/ to compare original reads with verified summaries.
Filter by asset, or add `--market crypto` / `--symbols XAUUSD,XAGUSD` to the
preview command. This path makes no API calls, sends no messages, writes no
live state, and never commits or rewrites the source history. Legacy Markdown
prices remain rounded; missing quotes, candle paths and precision are marked
unavailable, not reconstructed from AI prose. This is a reporting review, not
a historical strategy replay or profitability test.

New reports separate strategy status, directional bias, conditional plans and
engine-assessed entry levels. Only `ENTRY_ELIGIBLE` with current matched evidence
is described as an eligible entry, and never as an executed trade. This reporting
check does not alter the engine or its trading rules.

When `DEEPSEEK_API_KEY` is set, the model selects optional fact IDs from a verified
catalogue; mandatory status and evidence notices cannot be omitted. Free-form
model prose and legacy cached reads are not displayed as current analysis.
Malformed responses, unavailable AI, dry runs and unmatched cached reads use a
labelled deterministic summary. Repeated inputs are labelled as repeats, not
fresh confirmation. Gap disappearance is not treated as a fill; a recorded later
candle can establish a boundary touch only.

New history entries include a `.json` evidence sidecar with the raw strategy
result, full-precision price/quote, source, timestamps, setup/hourly candles,
gap events and read mode. Existing Markdown history is left untouched.

`report --dry-run` still fetches market data but does not call the AI or send
reports. The normal `report` command retains its existing send and history
commit/push behavior; use `--preview-history` for local review without either.
