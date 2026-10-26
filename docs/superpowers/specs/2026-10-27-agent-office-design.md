# Agent office — design (2026-10-27)

Original build inspired by the agents-office idea (no code copied; that repo's licence forbids
bundling). A local page where the screener pipeline is shown as five agents moving between desks
while a job runs, plus a read-only calendar of past report runs.

## Decisions
- `python -m screener office [--port 8765]` — stdlib `ThreadingHTTPServer` on 127.0.0.1 only.
- Run button jobs: `demo` (offline), `scan` (= `once`), `report` (= `report --dry-run`). The real
  `report` (Telegram + git push) is deliberately not exposed.
- One job at a time; a second `POST /run` gets 409. Unknown job gets 400.
- Job runs as a child `python -m screener …` with `SCREENER_PROGRESS=1`; it prints one JSON line
  per step to stderr via `shared.progress()` (no-op when unset). The server relays them on
  `GET /events` (SSE). Non-JSON stderr lines are relayed as `line` events (errors).
- Agents: Feed (fetch), Engine (`process`), Charts (PNG render), Analyst (report card / AI read),
  Publisher (`history.write`, `publish`). Page queues events and plays them ≥ ~450 ms apart.
- Calendar: `GET /calendar.json` parses `history/<SYM>/<YYYY-MM-DD_HHMMSS>.md`; a run = one stamp.
  Month + week views; clicking a run fetches `GET /calendar/<stamp>.json` with each symbol's
  signal, combined read and verified/AI read. Reloads after each job.

## Testing
- `tests/test_office.py`: history parsing/grouping, `progress()` on/off, job validation + 409.
- Playwright (MCP browser): start server, click Demo, confirm agents move and status returns to
  IDLE, open calendar, click a run, screenshot each stage.
