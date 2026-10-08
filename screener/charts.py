"""Render live 15-minute candlestick charts for top movers and send to Telegram.

Read-only: reads the screener journal for active signals, ranks symbols by recent
% move, fetches 15-minute candles (Binance for crypto, Alpaca IEX for stocks),
renders candlestick PNGs with mplfinance, and sends them to the configured
Telegram chat along with the current dashboard link. No broker orders.
"""
import io
import json
import os
import re
from pathlib import Path

from .shared import DataError, get_json, iso, send, timestamp
from . import pwchart
from .feeds import MarketData
from .runtime import Cache

BINANCE = "https://data-api.binance.vision/api/v3"
OANDA_BASE = "https://api-fxpractice.oanda.com"
ACTIVE = ("CONFIRMED", "DEVELOPING", "RETESTED")
CAP = 8  # max charts per run


def _fmt(x):
    if x >= 1000:
        return f"{x:,.1f}"
    if x >= 1:
        return f"{x:,.2f}"
    if x >= 0.01:
        return f"{x:.4f}"
    return f"{x:.6f}"


def results(store):
    out = {}
    for key, raw in store.db.execute("SELECT key, value FROM kv WHERE key LIKE 'result:%'"):
        parts = key.split(":")
        if len(parts) == 3 and parts[1] in ("stocks", "crypto", "metals"):
            out[(parts[1], parts[2])] = json.loads(raw)
    return out


def crypto_movers(symbols):
    rows = get_json(BINANCE + "/ticker/24hr", {"symbols": json.dumps(symbols, separators=(",", ":"))})
    return {r["symbol"]: float(r["priceChangePercent"]) for r in rows}


def stock_movers(symbols, headers):
    rows = get_json(
        "https://data.alpaca.markets/v2/stocks/snapshots",
        {"symbols": ",".join(symbols), "feed": "iex"},
        headers,
    )
    out = {}
    for sym, snap in rows.items():
        try:
            prev = float(snap["prevDailyBar"]["c"])
            last = float(snap["latestTrade"]["p"])
            out[sym] = (last - prev) / prev * 100 if prev else 0.0
        except (KeyError, TypeError, ValueError):
            out[sym] = 0.0
    return out


def metal_movers(symbols, headers):
    out = {}
    for sym in symbols:
        instrument = sym[:3] + "_" + sym[3:]
        try:
            raw = get_json(
                OANDA_BASE + "/v3/instruments/" + instrument + "/candles",
                {"granularity": "H1", "count": 25, "price": "M"},
                headers,
            )
            closes = [float(c["mid"]["c"]) for c in raw["candles"] if c.get("complete")]
            out[sym] = (closes[-1] - closes[0]) / closes[0] * 100 if len(closes) >= 2 else 0.0
        except (DataError, KeyError, TypeError, ValueError):
            out[sym] = 0.0
    return out


def metal_bars(symbol, headers, limit=192):
    instrument = symbol[:3] + "_" + symbol[3:]
    raw = get_json(
        OANDA_BASE + "/v3/instruments/" + instrument + "/candles",
        {"granularity": "M15", "count": limit, "price": "M"},
        headers,
    )
    return [
        dict(t=timestamp(c["time"]), o=float(c["mid"]["o"]), h=float(c["mid"]["h"]),
             l=float(c["mid"]["l"]), c=float(c["mid"]["c"]), v=float(c["volume"]))
        for c in raw["candles"]
    ]


def select(results, crypto_pct, stock_pct, metal_pct, cap=CAP):
    active = []
    for (market, sym), r in results.items():
        if r.get("status") in ACTIVE:
            if market == "crypto":
                pct = crypto_pct.get(sym, 0.0)
            elif market == "stocks":
                pct = stock_pct.get(sym, 0.0)
            else:
                pct = metal_pct.get(sym, 0.0)
            active.append((market, sym, pct, r.get("status"), r.get("side"), r))
    active.sort(key=lambda x: -abs(x[2]))
    picks = active[:cap]
    if len(picks) < cap:
        seen = {(m, s) for m, s, *_ in picks}
        movers = [("crypto", s, p) for s, p in crypto_pct.items()]
        movers += [("stocks", s, p) for s, p in stock_pct.items()]
        movers += [("metals", s, p) for s, p in metal_pct.items()]
        movers.sort(key=lambda x: -abs(x[2]))
        for m, s, p in movers:
            if len(picks) >= cap:
                break
            if (m, s) in seen:
                continue
            picks.append((m, s, p, "mover", None, results.get((m, s))))
            seen.add((m, s))
    return picks


def crypto_bars(symbol, limit=192):
    rows = get_json(BINANCE + "/klines", {"symbol": symbol, "interval": "15m", "limit": limit})
    return [
        dict(t=int(r[0]) / 1000, o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[5]))
        for r in rows
    ]


def stock_bars(symbol, headers, now, days=5):
    payload = get_json(
        "https://data.alpaca.markets/v2/stocks/bars",
        dict(
            symbols=symbol,
            timeframe="15Min",
            start=iso(now - days * 86400),
            end=iso(now),
            adjustment="split",
            feed="iex",
            limit=10000,
            sort="asc",
        ),
        headers,
    )
    rows = payload.get("bars", {}).get(symbol, [])
    return [
        dict(t=timestamp(r["t"]), o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]), v=float(r["v"]))
        for r in rows
    ]


def render(bars, symbol, signal=None, live=None):
    import matplotlib

    matplotlib.use("Agg")
    import mplfinance as mpf
    import pandas as pd

    df = pd.DataFrame(bars)
    df["Date"] = pd.to_datetime(df["t"], unit="s", utc=True)
    df = df.set_index("Date")[["o", "h", "l", "c", "v"]].rename(
        columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"}
    )
    last = df.iloc[-1]
    pct = (last["Close"] - df.iloc[0]["Open"]) / df.iloc[0]["Open"] * 100
    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit", wick="inherit", volume="inherit")
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", y_on_right=False)
    buf = io.BytesIO()
    plot_kwargs = dict(
        type="candle",
        style=style,
        volume=False,
        figsize=(9, 5.5),
        xrotation=0,
        datetime_format="%m-%d %H:%M",
        tight_layout=True,
        savefig=dict(fname=buf, dpi=110, bbox_inches="tight"),
    )
    lines, colors, title_parts = [], [], [symbol]
    if signal and signal.get("entry"):
        side = signal.get("side")
        action = "BUY" if side == "LONG" else "SELL"
        active = signal.get("status") in ACTIVE
        lines.append(signal["entry"])
        colors.append("#26a69a" if side == "LONG" else "#ef5350")
        if active:
            if signal.get("stop"):
                lines.append(signal["stop"])
                colors.append("#ef5350")
            if signal.get("target"):
                lines.append(signal["target"])
                colors.append("#26a69a")
            title_parts.append(f"{action} {signal['entry']:.2f}")
        else:
            title_parts.append(f"watch {action} @ {signal['entry']:.2f}")
    if live and live.get("price"):
        lines.append(live["price"])
        colors.append("#ff9800")
        title_parts.append(f"LIVE {'↑' if live['side'] == 'LONG' else '↓'} {live['price']:.2f}")
    if lines:
        plot_kwargs["hlines"] = dict(
            hlines=lines, colors=colors, linestyle="--", linewidths=[1.1] * len(lines)
        )
    if len(title_parts) > 1:
        plot_kwargs["title"] = " · ".join(title_parts)
    mpf.plot(df, **plot_kwargs)
    buf.seek(0)
    return buf.read(), float(last["Close"]), float(pct)


def signal_of(r):
    if not r:
        return None
    return dict(status=r.get("status"), side=r.get("side"),
                entry=r.get("plan_entry"), stop=r.get("plan_stop"),
                target=r.get("plan_target"))


def caption(symbol, tag, sig, last, pct, live=None):
    if live and live.get("price"):
        side = live.get("side")
        action = "BUY" if side == "LONG" else "SELL"
        arrow = "↑" if side == "LONG" else "↓"
        trig = f" · trigger {_fmt(sig['entry'])}" if sig and sig.get("entry") else ""
        return f"🔴 LIVE BREAKOUT {symbol} · {action} {arrow} now {_fmt(live['price'])}{trig} · 1H · {_fmt(last)} · {pct:+.2f}%"
    base = f"{symbol} · {tag}"
    if sig and sig.get("entry"):
        side = sig.get("side")
        action = "BUY" if side == "LONG" else "SELL"
        if sig.get("status") in ACTIVE:
            base += f" · {action} entry {_fmt(sig['entry'])}"
            if sig.get("stop"):
                base += f" · stop {_fmt(sig['stop'])}"
            if sig.get("target"):
                base += f" · target {_fmt(sig['target'])}"
        else:
            base += f" · watch {action} @ {_fmt(sig['entry'])}"
    return f"{base} · 1H · {_fmt(last)} · {pct:+.2f}%"


def live_breakout(r, price, buffer=0.1):
    upper, lower, atr = r.get("upper"), r.get("lower"), r.get("atr")
    if not (upper and lower and atr and price):
        return None
    if price > upper + buffer * atr:
        return "LONG"
    if price < lower - buffer * atr:
        return "SHORT"
    return None


def live_prices(config, headers, oanda):
    out = {}
    syms = config["crypto"]["symbols"]
    if syms:
        rows = get_json(BINANCE + "/ticker/price", {"symbols": json.dumps(syms, separators=(",", ":"))})
        for r in rows:
            out[("crypto", r["symbol"])] = float(r["price"])
    for sym in config["metals"]["symbols"]:
        instrument = sym[:3] + "_" + sym[3:]
        try:
            raw = get_json(OANDA_BASE + "/v3/instruments/" + instrument + "/candles",
                           {"granularity": "M1", "count": 1, "price": "M"}, oanda)
            out[("metals", sym)] = float(raw["candles"][-1]["mid"]["c"])
        except (DataError, KeyError, TypeError, ValueError, IndexError):
            pass
    syms = config["stocks"]["symbols"]
    if syms:
        rows = get_json("https://data.alpaca.markets/v2/stocks/snapshots",
                        {"symbols": ",".join(syms), "feed": "iex"}, headers)
        for sym, snap in rows.items():
            try:
                out[("stocks", sym)] = float(snap["latestTrade"]["p"])
            except (KeyError, TypeError, ValueError):
                pass
    return out


def live_scan(config, store, prices, token, chat, now, buffer=0.1):
    res = results(store)
    pings = []
    for (market, sym), price in prices.items():
        r = res.get((market, sym))
        if not r:
            continue
        side = live_breakout(r, price, buffer)
        key = "live:" + market + ":" + sym
        prev = store.get(key)
        if side and r.get("status") not in ("CONFIRMED", "RETESTED"):
            if not prev or prev.get("side") != side:
                store.put(key, {"side": side, "at": now})
                pings.append((market, sym, side, price, r))
        elif prev:
            store.db.execute("DELETE FROM kv WHERE key=?", (key,))
    for market, sym, side, price, r in pings:
        action = "BUY" if side == "LONG" else "SELL"
        arrow = "↑" if side == "LONG" else "↓"
        level = r.get("upper") if side == "LONG" else r.get("lower")
        send(token, chat,
             f"🔴 LIVE BREAKOUT {sym} · {action} {arrow} now {_fmt(price)} · trigger {_fmt(level)} · confirm on close",
             None)
    return pings


def dashboard_url():
    p = Path(".screener") / "tunnel.log"
    if p.exists():
        m = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", p.read_text(errors="ignore"))
        if m:
            return m[-1]
    return None


def run(config, store, now):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise DataError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    headers = {
        "APCA-API-KEY-ID": os.getenv("APCA_API_KEY_ID"),
        "APCA-API-SECRET-KEY": os.getenv("APCA_API_SECRET_KEY"),
    }
    oanda_headers = {"Authorization": "Bearer " + os.getenv("OANDA_API_TOKEN", "")}
    data = MarketData(config, Cache(store), now)
    from . import report  # lazy: report imports charts, avoid module-level cycle
    buffer = config["strategy"].get("breakout_atr", 0.1)
    prices = live_prices(config, headers, oanda_headers)
    try:
        live_scan(config, store, prices, token, chat, now, buffer)
    except (DataError, OSError, ValueError, KeyError, TypeError):
        pass  # live ping is best-effort; charts still run
    picks = select(
        results(store),
        crypto_movers(config["crypto"]["symbols"]),
        stock_movers(config["stocks"]["symbols"], headers),
        metal_movers(config["metals"]["symbols"], oanda_headers),
    )
    if not picks:
        raise DataError("No movers or active signals to chart")
    failures = []
    for market, symbol, pct, tag, side, r in picks:
        try:
            if market == "crypto":
                bars = pwchart.crypto_bars_1h(symbol)
                source = "binance"
            elif market == "stocks":
                bars = pwchart.stock_bars_1h(symbol, headers, now)
                source = "alpaca iex"
            else:
                bars = pwchart.metal_bars_1h(symbol, oanda_headers)
                source = "oanda practice"
            if len(bars) < 2:
                failures.append(f"{symbol}: no bars")
                continue
            sig = signal_of(r)
            live = None
            price = prices.get((market, symbol))
            if price and r:
                lside = live_breakout(r, price, buffer)
                if lside and r.get("status") not in ("CONFIRMED", "RETESTED"):
                    live = dict(side=lside, price=price)
            png = pwchart.render_png(pwchart.svg(symbol, bars, r or {}, market, source, now))
            last = bars[-1]["c"]
            chart_pct = (bars[-1]["c"] - bars[0]["o"]) / bars[0]["o"] * 100
            try:
                if market == "crypto":
                    bundle = data.crypto(symbol)
                elif market == "stocks":
                    bundle = data.stock(symbol)
                else:
                    bundle = data.metal(symbol)
                setup, hourly = bundle["setup"], bundle["hourly"]
                if len(setup) < 55 or len(hourly) < 55:
                    continue
                cur = (bundle.get("quote") or {}).get("price") or hourly[-1]["close"]
                _, fields = report.card(r or {}, symbol, market, setup, hourly, cur, store, dry_run=False, cached_read=True)
                analysis = report.analysis_html(fields)
                png = pwchart.render_png(pwchart.svg(symbol, bars, r or {}, market, source, now, analysis))
            except (DataError, OSError, ValueError, KeyError, TypeError, IndexError):
                pass  # analysis is best-effort; keep the plain chart
        except (DataError, OSError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"{symbol}: {exc}")
            continue
        label = caption(symbol, tag, sig, last, chart_pct, live)
        send(token, chat, label, png)
    summary = "SCREENER · 1H pivots"
    link = dashboard_url()
    if link:
        summary += "\nDashboard: " + link
    if failures:
        summary += "\nSkipped: " + ", ".join(failures)
    send(token, chat, summary, None)
