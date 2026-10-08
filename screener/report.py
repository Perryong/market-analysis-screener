"""Rich per-symbol signal cards: symbol · side · direction · combined · FVG · AI read.

Read-only analysis: reads the screener journal for signals, reuses the cached setup
(4H crypto/metals, daily stocks) + 1H candles via MarketData, computes an EMA50
direction, a 1H trend/momentum/FVG confluence verdict, and a DeepSeek AI read — the
previous read is fed back in so each run compares against its own history. Sends one
text card per symbol to the configured Telegram chat. No broker orders.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

from .shared import DataError, send
from .feeds import MarketData
from .charts import results, signal_of, crypto_bars, stock_bars, metal_bars
from .runtime import Cache

SGT = timezone(timedelta(hours=8))
ACTIVE = ("CONFIRMED", "DEVELOPING", "RETESTED", "ENTRY_ELIGIBLE")

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def fmt(v):
    if v is None:
        return "—"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:,.4f}"


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
    sys_msg = (
        "You are a concise market analyst. Write a short 'AI read' (3-5 sentences) "
        "grounded ONLY in the data given. State the bias, the key levels, and the "
        "invalidation condition. When a previous read is given, compare against it: "
        "say what changed (level breaks, trend flips, gap fills) and confirm or revise "
        "the bias. Start directly with the analysis text — no title, no header, no "
        "markdown. No hedging, no disclaimers."
    )
    user = f"Symbol: {symbol}\nPrevious read: {prev or '(none)'}\nNew data:\n{ctx}\n\nWrite the updated AI read."
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}],
        "temperature": 0.4,
        "max_tokens": 320,
    }
    req = urllib.request.Request(
        DEEPSEEK_BASE + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + DEEPSEEK_KEY},
    )
    r = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return r["choices"][0]["message"]["content"].strip()


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


def card(r, symbol, market, setup, hourly, cur, store, dry_run, pct=None):
    sig = signal_of(r)
    status = r.get("status")
    side = (sig or {}).get("side")
    upper, lower = r.get("upper"), r.get("lower")
    entry = (sig or {}).get("entry")
    stop = (sig or {}).get("stop")
    target = (sig or {}).get("target")

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
        top = f"{symbol} · {status} · {side_label} entry {fmt(entry)} · stop {fmt(stop)} · target {fmt(target)} · 15m · {fmt(cur)} · {pct_txt}"
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
        f"{datetime.now(SGT):%Y-%m-%d %H:%M:%S} +08",
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
        lines.append(f"Setup: {'broke above' if side == 'LONG' else 'broke below'} {fmt(lvl)}")
    elif status == "WATCHING" and side in ("LONG", "SHORT"):
        lvl = upper if side == "LONG" else lower
        lines.append(f"Setup: watching {'breakout' if side == 'LONG' else 'breakdown'} vs {fmt(lvl)}")
    else:
        lines.append(f"Setup: {status}")

    lines.append("")
    lines.append("Last completed candles (SGT):")
    lines.append(f"1H: {datetime.fromtimestamp(closed['end'], SGT):%Y-%m-%d %H:%M:%S} +08")
    lines.append(f"{tf}: {datetime.fromtimestamp(setup[-1]['end'], SGT):%Y-%m-%d %H:%M:%S} +08")
    lines.append("")
    lines.append("Analysis only · No orders placed")
    lines.append(tv_link(symbol, market))

    ctx = (
        f"{symbol} · {side_label} ({status})\n"
        f"{tf} direction: {d_txt} · Combined ({tf}+1H): {comb}\n"
        f"Price: {fmt(cur)} · Upper pivot {fmt(upper)} · Lower pivot {fmt(lower)}\n"
        f"1H trend {'up' if trend_up else 'down'} · last 1H {fmt(closed['open'])}→{fmt(closed['close'])} ({chg:+.2f}%)\n"
        f"Open FVGs: {[(t, round(bot, 6), round(top_, 6)) for t, bot, top_ in fvgs_near]}\n"
        f"Entry {fmt(entry)} · Stop {fmt(stop)} · Target {fmt(target)} · Regime {r.get('regime')}"
    )
    key = f"airead:{market}:{symbol}"
    prev = store.get(key)
    read = None if dry_run else ai_read(symbol, prev, ctx)
    if read:
        store.put(key, read)

    lines.append("")
    if prev:
        lines.append("Previous read:")
        lines.append(prev)
        lines.append("")
    lines.append("AI read:")
    lines.append(read if read else ("[dry run — AI read skipped]" if dry_run else "[AI read unavailable]"))
    return "\n".join(lines)


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
            text = card(r, sym, market, setup, hourly, cur, store, dry_run, pct)
            if dry_run:
                print(text)
                print("\n" + "=" * 60 + "\n")
            else:
                send(token, chat, text, None)
                sent += 1
        except (DataError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            failures.append(f"{sym}: {exc}")
    if not dry_run and sent:
        summary = f"SCREENER REPORT · {sent} cards"
        if failures:
            summary += "\nSkipped: " + ", ".join(failures)
        send(token, chat, summary, None)
    return failures
