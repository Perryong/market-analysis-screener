"""Rich per-symbol signal cards: symbol · side · direction · combined · FVG · AI read.

Read-only analysis: reads the screener journal for signals, reuses the cached setup
(4H crypto/metals, daily stocks) + 1H candles via MarketData, computes an EMA50
direction, a 1H trend/momentum/FVG confluence verdict, and a DeepSeek AI read — the
AI selects verified facts, while comparisons use recorded evidence rather than
previous prose. Sends one card per symbol to the configured Telegram chat. No broker orders.
"""

import html
import json
import os
import urllib.request
import time
from copy import deepcopy
from datetime import datetime, timezone, timedelta

from .shared import DataError, progress, send
from .reporting import number as fmt, fact_catalog, verified_read, evidence_notes, gap_events, evidence_signature
from .report_preview import preview_history
from .feeds import MarketData
from .charts import results, signal_of, crypto_bars, stock_bars, metal_bars
from .runtime import Cache
from . import pwchart
from . import history

SGT = timezone(timedelta(hours=8))
ACTIVE = ("CONFIRMED", "RETESTED", "ENTRY_ELIGIBLE")

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def ema(vals, n):
    if not vals:
        return 0.0
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def open_fvgs(bars, lookback=30):
    """Open 3-candle fair-value gaps in the last `lookback` bars (dict bars)."""
    n = len(bars)
    out = []
    for i in range(1, n - 1):
        if i < n - lookback:
            continue
        p, nx = bars[i - 1], bars[i + 1]
        if p["high"] < nx["low"]:
            bot, top, typ = p["high"], nx["low"], "bull"
        elif p["low"] > nx["high"]:
            bot, top, typ = nx["high"], p["low"], "bear"
        else:
            continue
        if any((b["high"] >= bot and b["low"] <= bot) or (b["low"] <= top and b["high"] >= top)
               for b in bars[i + 2:]):
            continue
        out.append((typ, bot, top))
    return out


def direction(bars):
    closes = [b["close"] for b in bars]
    e = ema(closes, 50)
    return closes[-1] > e, e


def hourly_signal(hourly, cur, fvgs_near):
    e = ema([b["close"] for b in hourly[-60:]], 50)
    trend_up = cur > e
    closed = hourly[-1]
    mom_up = closed["close"] >= closed["open"]
    overhead_bear = any(t == "bear" and bot > cur for t, bot, _ in fvgs_near)
    below_bull = any(t == "bull" and top < cur for t, _, top in fvgs_near)
    if trend_up and mom_up and not overhead_bear:
        return "BUY", "trend↑ + momentum↑, no gap overhead"
    if (not trend_up) and (not mom_up) and not below_bull:
        return "SELL", "trend↓ + momentum↓, no support gap below"
    if trend_up and mom_up and overhead_bear:
        return "NO", "trend↑ + momentum↑ but bearish gap overhead"
    if (not trend_up) and (not mom_up) and below_bull:
        return "NO", "trend↓ + momentum↓ but support gap below"
    if trend_up and not mom_up:
        return "NO", "trend↑ but momentum↓"
    if (not trend_up) and mom_up:
        return "NO", "trend↓ but momentum↑"
    return "NO", "mixed"


def combined(d_up, s1):
    if d_up and s1 == "BUY":
        return "BUY"
    if (not d_up) and s1 == "SELL":
        return "SELL"
    return "HOLD"


def ai_read(symbol, prev, ctx):
    if not DEEPSEEK_KEY:
        return None
    sys_msg = ('Select 1 to 5 useful optional fact IDs from the supplied verified catalogue. '
               'Return only JSON: {"fact_ids": ["price"]}. No other keys or prose. '
               'Mandatory strategy, eligibility and evidence facts are always rendered by the application. '
               'Never invent IDs, price events, fills, entries or instructions.')
    # Previous model prose is deliberately excluded: only recorded facts are evidence.
    user = json.dumps(dict(symbol=symbol, facts=ctx), allow_nan=False)
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 320,
    }
    req = urllib.request.Request(
        DEEPSEEK_BASE + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + DEEPSEEK_KEY},
    )
    r = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return json.loads(r["choices"][0]["message"]["content"])


def tv_link(symbol, market):
    if market == "crypto":
        prefix, interval = "BINANCE:", "240"
    elif market == "metals":
        prefix, interval = "OANDA:", "240"
    else:
        prefix, interval = "", "1D"
    return f"https://www.tradingview.com/chart/?symbol={prefix}{symbol}&interval={interval}"


def pct_15m(market, symbol, headers, oanda, now):
    """% change over the 15-minute chart window (first open → last close), matching the chart caption."""
    try:
        if market == "crypto":
            bars = crypto_bars(symbol)
        elif market == "metals":
            bars = metal_bars(symbol, oanda)
        else:
            bars = stock_bars(symbol, headers, now)
        if len(bars) < 2:
            return None
        return (bars[-1]["c"] - bars[0]["o"]) / bars[0]["o"] * 100
    except (DataError, OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def card(r, symbol, market, setup, hourly, cur, store, dry_run, pct=None, cached_read=False,
         *, now=None, bundle=None):
    now = time.time() if now is None else now
    bundle = bundle or {}
    sig = signal_of(r)
    status = r.get("status")
    side = (sig or {}).get("side")
    upper, lower = r.get("upper"), r.get("lower")
    entry = (sig or {}).get("entry")
    stop = (sig or {}).get("stop")
    target = (sig or {}).get("target")
    if status == 'ENTRY_ELIGIBLE':
        entry, stop, target = (r.get(k) for k in ('entry', 'stop', 'target'))

    tf = "1D" if market == "stocks" else "4H"
    dir_label = "Daily direction" if market == "stocks" else "4H direction"
    d_up, _ = direction(setup)
    d_txt = "Bullish" if d_up else "Bearish"

    fvgs = open_fvgs(hourly, 30)
    fvgs_near = [f for f in fvgs if min(f[1], f[2]) <= cur <= max(f[1], f[2]) or abs(f[1] - cur) / cur < 0.03]
    s1, why1 = hourly_signal(hourly, cur, fvgs_near)
    comb = combined(d_up, s1)

    side_label = "BUY" if side == "LONG" else ("SELL" if side == "SHORT" else "—")
    pct_txt = f"{pct:+.2f}%" if pct is not None else "—"
    if entry and status in ACTIVE and side in ("LONG", "SHORT"):
        label = 'engine-assessed' if status == 'ENTRY_ELIGIBLE' else 'conditional plan'
        top = f"{symbol} · {status} · {side_label} {label} entry {fmt(entry)} · stop {fmt(stop)} · target {fmt(target)} · 15m · {fmt(cur)} · {pct_txt}"
    elif status == "WATCHING" and side in ("LONG", "SHORT"):
        lvl = upper if side == "LONG" else lower
        top = f"{symbol} · WATCHING · watch {side_label} @ {fmt(lvl)} · 15m · {fmt(cur)} · {pct_txt}"
    else:
        top = f"{symbol} · {status} · 15m · {fmt(cur)} · {pct_txt}"

    if comb != "HOLD":
        comb_txt = f"COMBINED ({tf}+1H): {comb} — {'bullish' if d_up else 'bearish'} {tf} + {why1}"
    else:
        comb_txt = f"COMBINED ({tf}+1H): HOLD — no confluence"

    e1 = ema([b["close"] for b in hourly[-60:]], 50)
    trend_up = cur > e1
    closed = hourly[-1]
    chg = (closed["close"] / closed["open"] - 1) * 100

    lines = [
        top,
        f"{datetime.fromtimestamp(now, SGT):%Y-%m-%d %H:%M:%S} +08",
        "",
        f"{dir_label}: {d_txt}",
        "",
        comb_txt,
        "",
        f"Price: {fmt(cur)}",
        f"Upper pivot: {fmt(upper)} · Lower pivot: {fmt(lower)}",
        f"1H trend: {'↑ (up)' if trend_up else '↓ (down)'}",
        f"1H closed: {fmt(closed['open'])} → {fmt(closed['close'])} ({chg:+.2f}%)",
    ]
    for t, bot, top_ in fvgs_near[:3]:
        lines.append(f"Open FVG ({t}): {fmt(bot)}–{fmt(top_)}")
    if not fvgs_near:
        lines.append("no open FVG near price")

    lines.append("")
    if status in ACTIVE and side in ("LONG", "SHORT"):
        lvl = upper if side == "LONG" else lower
        setup_txt = f"{'broke above' if side == 'LONG' else 'broke below'} {fmt(lvl)}"
    elif status == "WATCHING" and side in ("LONG", "SHORT"):
        lvl = upper if side == "LONG" else lower
        setup_txt = f"watching {'breakout' if side == 'LONG' else 'breakdown'} vs {fmt(lvl)}"
    else:
        setup_txt = f"{status}"
    lines.append(f"Setup: {setup_txt}")

    lines.append("")
    lines.append("Last completed candles (SGT):")
    last_1h = f"{datetime.fromtimestamp(closed['end'], SGT):%Y-%m-%d %H:%M:%S} +08"
    last_tf = f"{datetime.fromtimestamp(setup[-1]['end'], SGT):%Y-%m-%d %H:%M:%S} +08"
    lines.append(f"1H: {last_1h}")
    lines.append(f"{tf}: {last_tf}")
    lines.append("")
    lines.append("Analysis only · No orders placed")
    lines.append(tv_link(symbol, market))

    key = f"report_evidence:v1:{market}:{symbol}"
    previous = store.get(key) or {}
    quote = bundle.get('quote') or {}
    price_kind = 'quote' if quote else 'candle_close'
    if market == 'metals':
        price_kind = 'candle_close'  # Current metals feed uses the last H1 close, not a quote.
    snapshot = deepcopy(dict(schema_version=1, observed_at=now, symbol=symbol, market=market,
                    full_precision=True, source=bundle.get('source', r.get('source', 'unknown')),
                    market_open=bundle.get('market_open'), quote=quote,
                    price=dict(value=cur, time=quote.get('time') if quote else closed['end'], kind=price_kind),
                    result=r, setup=setup, hourly=hourly,
                    candle_ends=dict(setup=setup[-1]['end'], hourly=closed['end']),
                    tf=tf, direction=d_txt, combined=comb, trend_up=trend_up,
                    fvgs_near=fvgs_near, previous=previous.get('evidence')))
    evidence = {k: snapshot[k] for k in ('price', 'candle_ends', 'fvgs_near')}
    # Ignore scan/quote timestamps when deciding whether market facts changed.
    signature = evidence_signature(snapshot)
    snapshot['unchanged'] = previous.get('signature') == signature
    mandatory, optional = fact_catalog(snapshot)
    selection, error = None, None
    if snapshot['unchanged']:
        selection = previous.get('selection')
    elif not dry_run and not cached_read and DEEPSEEK_KEY:
        try:
            selection = ai_read(symbol, None, dict(mandatory=mandatory, optional=optional))
            if selection is None:
                selection = {}  # A model returning JSON null is invalid, not an absent AI call.
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            error = 'fallback: AI unavailable or malformed response'
    read, mode = verified_read(snapshot, selection)
    if error:
        mode = error
    snapshot['gap_events'] = gap_events(snapshot)
    snapshot['read_mode'] = mode
    snapshot['model'] = DEEPSEEK_MODEL if mode == 'AI-selected verified facts' else None
    snapshot['selection'] = selection if mode == 'AI-selected verified facts' else None
    data_note = '; '.join(evidence_notes(snapshot)) or 'Current matched evidence'
    if not dry_run and not cached_read:
        store.put(key, dict(signature=signature, evidence=evidence, selection=snapshot['selection']))
    prev = None

    lines.append("")
    if prev:
        lines.append("Previous read:")
        lines.append(prev)
        lines.append("")
    lines.append(f"Verified read ({mode}):")
    lines.append(read)
    fields = dict(
        top=top, dir_label=dir_label, d_txt=d_txt, d_up=d_up, comb_txt=comb_txt,
        price=fmt(cur), upper=fmt(upper), lower=fmt(lower), trend_up=trend_up,
        closed_open=fmt(closed["open"]), closed_close=fmt(closed["close"]), chg=chg,
        fvgs_near=fvgs_near, setup_txt=setup_txt, last_1h=last_1h, last_tf=last_tf,
        prev_read=prev, ai_read=read, tv_link=tv_link(symbol, market), tf=tf,
        snapshot=snapshot, read_mode=mode, data_note=data_note,
    )
    return "\n".join(lines), fields


def analysis_html(f):
    """Render the report-card analysis as an HTML block for embedding in the chart image."""
    esc = html.escape
    comb_val = f["comb_txt"].split(": ", 1)[-1]
    fvg_txt = " · ".join(f"{t}: {fmt(bot)}–{fmt(top)}" for t, bot, top in f["fvgs_near"]) or "no open FVG near price"
    rows = [
        (f["dir_label"], f["d_txt"], "bull" if f["d_up"] else "bear"),
        (f"COMBINED ({f['tf']}+1H)", comb_val, ""),
        ("Price", f["price"], ""),
        ("Pivots", f"Upper {f['upper']} · Lower {f['lower']}", ""),
        ("1H trend", "↑ (up)" if f["trend_up"] else "↓ (down)", "bull" if f["trend_up"] else "bear"),
        ("1H closed", f"{f['closed_open']} → {f['closed_close']} ({f['chg']:+.2f}%)", ""),
        ("Open FVG", fvg_txt, ""),
        ("Setup", f["setup_txt"], ""),
        ("Evidence", f.get('data_note', 'Unavailable'), ""),
    ]
    body = "".join(
        f'<div class="row"><span class="k">{esc(k)}</span><span class="v {cls}">{esc(v)}</span></div>'
        for k, v, cls in rows
    )
    ai = f'<p>{esc(f["ai_read"])}</p>' if f["ai_read"] else ""
    prev = (f'<h3>Previous read</h3><p class="prev">{esc(f["prev_read"])}</p>' if f["prev_read"] else "")
    return (
        '<div class="analysis"><h3>Analysis</h3>' + body
        + '<div class="row"><span class="k">Last completed</span><span class="v">'
        + f"1H {esc(f['last_1h'])} · {f['tf']} {esc(f['last_tf'])}</span></div>"
        + prev + '<h3>Verified read</h3><p class="meta">' + esc(f.get('read_mode', '')) + '</p>' + ai
        + '<p class="meta">Analysis only · No orders placed</p>'
        + f'<p class="meta">{esc(f["tv_link"])}</p></div>'
    )


def run(config, store, now, dry_run=False, only=None, symbols=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not dry_run and (not token or not chat):
        raise DataError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    data = MarketData(config, Cache(store), now)
    headers = {"APCA-API-KEY-ID": os.getenv("APCA_API_KEY_ID"), "APCA-API-SECRET-KEY": os.getenv("APCA_API_SECRET_KEY")}
    oanda = {"Authorization": "Bearer " + os.getenv("OANDA_API_TOKEN", "")}
    res = results(store)
    failures, sent = [], 0
    for (market, sym), r in sorted(res.items()):
        if r.get("status") == "DATA_UNAVAILABLE":
            continue
        if only and market != only:
            continue
        if symbols and sym not in symbols:
            continue
        try:
            if market == "crypto":
                bundle = data.crypto(sym)
            elif market == "metals":
                bundle = data.metal(sym)
            else:
                bundle = data.stock(sym)
            setup, hourly = bundle["setup"], bundle["hourly"]
            if len(setup) < 55 or len(hourly) < 55:
                failures.append(f"{sym}: insufficient candles")
                continue
            cur = (bundle.get("quote") or {}).get("price") or hourly[-1]["close"]
            pct = pct_15m(market, sym, headers, oanda, now)
            text, fields = card(r, sym, market, setup, hourly, cur, store, dry_run, pct, now=now, bundle=bundle)
            progress('analyst','read',sym,market=market,status=fields['comb_txt'].split(': ',1)[-1].split(' ')[0])
            if dry_run:
                print(text)
                print("\n" + "=" * 60 + "\n")
                continue
            if market == "crypto":
                bars = pwchart.crypto_bars_1h(sym)
                source = "binance"
            elif market == "stocks":
                bars = pwchart.stock_bars_1h(sym, headers, now)
                source = "alpaca iex"
            else:
                bars = pwchart.metal_bars_1h(sym, oanda)
                source = "oanda practice"
            if len(bars) < 2:
                failures.append(f"{sym}: no 1H bars")
                continue
            progress('charts','draw',sym,market=market)
            png = pwchart.render_png(pwchart.svg(sym, bars, r, market, source, now, analysis_html(fields)))
            history.write(sym, fields, png, now)
            send(token, chat, fields["top"], png)
            sent += 1
        except (DataError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            failures.append(f"{sym}: {exc}")
    git_err = history.git_commit(now, sent) if (not dry_run and sent) else None
    if not dry_run and sent:
        summary = f"SCREENER REPORT · {sent} charts"
        if failures:
            summary += "\nSkipped: " + ", ".join(failures)
        if git_err:
            summary += "\n" + git_err
        send(token, chat, summary, None)
    return failures
