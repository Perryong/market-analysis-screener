"""Pivot-watch style chart: dark 1H candles with dashed pivot + T1/T2 target lines.

Ported from market-pivot-watch's `level_chart` (site.py) + `render_chart`
(telegram.py). Renders an SVG (dark theme, teal #63e3c4 bullish / pink #ffa5ae
bearish) and screenshots it to PNG with headless Chromium. Read-only.
"""

import html
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .shared import DataError, get_json, iso, timestamp

SGT = timezone(timedelta(hours=8))
TEAL = "#63e3c4"
PINK = "#ffa5ae"
MUTED = "#a4b5c8"

BINANCE = "https://data-api.binance.vision/api/v3"
OANDA_BASE = "https://api-fxpractice.oanda.com"

CSS = (
    ":root{color-scheme:dark}body{margin:0;background:#0b111b;color:#ecf2f9;"
    "font:16px/1.6 system-ui,-apple-system,sans-serif;padding:20px}"
    "h2{font-size:27px;margin:0}h3{font-size:19px}"
    ".meta{color:#a4b5c8;font-size:13px}"
    ".level-chart{margin:24px 0;padding:18px;border:1px solid #2b3b50;border-radius:10px;background:#0e1723}"
    ".chart-scroll{overflow-x:auto}"
    ".level-chart svg{display:block;width:100%;min-width:760px;font:14px system-ui}"
    ".analysis{margin:24px 0;padding:18px;border:1px solid #2b3b50;border-radius:10px;background:#0e1723}"
    ".analysis h3{font-size:17px;margin:18px 0 6px}"
    ".analysis .row{display:flex;gap:14px;padding:3px 0;font-size:15px}"
    ".analysis .k{color:#a4b5c8;min-width:200px;flex:none}"
    ".analysis .v.bull{color:#63e3c4}.analysis .v.bear{color:#ffa5ae}"
    ".analysis p{font-size:15px;margin:5px 0}"
    ".analysis .prev{color:#8fa3bd}"
)


def targets(r):
    upper, lower = r.get("upper"), r.get("lower")
    if not upper or not lower or upper <= lower:
        return None, None
    w = upper - lower
    return [upper + w, upper + 2 * w], [lower - w, lower - 2 * w]


def heading(r):
    status = r.get("status") or "WAIT"
    side = r.get("side")
    long = side == "LONG"
    short = side == "SHORT"
    if status == "WATCHING":
        return "WAIT — No confirmed entry"
    if status == "CONFIRMED":
        return f"{'BUY' if long else 'SELL'} — {'Bullish breakout' if long else 'Bearish breakout'}"
    if status == "RETESTED":
        return f"{'BUY' if long else 'SELL'} — Retest confirmed"
    if status == "DEVELOPING":
        return f"{'BUY' if long else 'SELL'} — Developing setup"
    if status == "ENTRY_ELIGIBLE":
        return f"{'BUY' if long else 'SELL'} — Entry eligible"
    if status in ("EXPIRED", "MISSED", "INVALIDATED"):
        return f"WAIT — Setup {status.lower()}"
    return "WAIT — No confirmed entry"


def _clock(ts):
    return datetime.fromtimestamp(float(ts), SGT).strftime("%Y-%m-%d %H:%M:%S")


def svg(symbol, bars, r, market, source, checked_at, analysis_html=None):
    """Return the full SVG chart fragment (h2 + meta + level-chart svg)."""
    bullish, bearish = targets(r)
    levels = [("BUY retest", r.get("upper"), TEAL),
              ("SHORT retest", r.get("lower"), PINK)]
    if bullish:
        levels += [(f"Buy T{i + 1}", v, TEAL) for i, v in enumerate(bullish)]
    if bearish:
        levels += [(f"Short T{i + 1}", v, PINK) for i, v in enumerate(bearish)]
    levels = [(l, v, c) for l, v, c in levels if v]

    lows = [b["l"] for b in bars] + [v for _, v, _ in levels]
    highs = [b["h"] for b in bars] + [v for _, v, _ in levels]
    low, high = min(lows), max(highs)
    span = high - low
    if span <= 0:
        raise ValueError("Empty price scale")

    def y(v):
        return 400 - (v - low) / span * 360

    duration = 3600
    t0 = bars[0]["t"]
    step = 780 / ((bars[-1]["t"] - t0) / duration + 1)
    shapes = []
    for b in bars:
        x = 20 + ((b["t"] - t0) / duration + 0.5) * step
        color = TEAL if b["c"] >= b["o"] else PINK
        shapes.append(
            f'<g fill="{color}" stroke="{color}">'
            f'<title>{_clock(b["t"])} | O {b["o"]:,.2f} H {b["h"]:,.2f} L {b["l"]:,.2f} C {b["c"]:,.2f}</title>'
            f'<line x1="{x}" x2="{x}" y1="{y(b["h"])}" y2="{y(b["l"])}"/>'
            f'<rect x="{x - step * 0.3}" y="{y(max(b["o"], b["c"]))}" width="{step * 0.6}" '
            f'height="{max(1, abs(y(b["o"]) - y(b["c"])))}"/></g>'
        )
    daily = market == "stocks"
    for label, value, color in levels:
        exit_note = {
            "BUY retest": "SELL / exit long on " + ("daily" if daily else "4H") + " close below",
            "SHORT retest": "BUY / exit short on " + ("daily" if daily else "4H") + " close above",
        }.get(label)
        subtitle = f'<tspan x="825" dy="15" font-size="11">{exit_note}</tspan>' if exit_note else ""
        shapes.append(
            f'<line x1="20" x2="810" y1="{y(value)}" y2="{y(value)}" stroke="{color}" stroke-dasharray="6 4"/>'
            f'<text x="825" y="{y(value) + 4}" fill="{color}">{label} {value:,.2f}{subtitle}</text>'
        )
    esc = html.escape
    close_time = bars[-1]["t"] + duration
    meta1 = (
        f"{esc(source)} · Completed candles through {_clock(close_time)} +08. "
        "Snapshot at the analysis check; refreshes with each report. Fixed pivot levels are shown "
        "across the chart for reference, not as historical signals. Hover a candle for OHLC."
    )
    meta2 = (
        "Entries require a breakout and later completed retest: hold above the upper pivot for BUY, "
        "reject below the lower pivot for SHORT. Exit rules apply only to the corresponding tracked setup "
        "and existing position; these are conditional levels, not buy/sell-now instructions."
    )
    return (
        f'<h2>{esc(symbol)} · {esc(heading(r))}</h2>'
        f'<p class="meta">Snapshot: {_clock(checked_at)} +08</p>'
        f'<div class="level-chart"><h3>1H candles with pivots and targets</h3>'
        f'<p class="meta">{meta1}</p><p class="meta">{meta2}</p>'
        f'<div class="chart-scroll"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 450">'
        f'<title>{esc(symbol)} — 1H candles and conditional levels</title>'
        + "".join(shapes)
        + f'<text x="20" y="440" fill="{MUTED}">{_clock(t0)}</text>'
        f'<text x="800" y="440" text-anchor="end" fill="{MUTED}">{_clock(close_time)}</text>'
        f'</svg></div></div>'
        + (analysis_html or "")
    )


def _trim(data, bg=(11, 17, 27), pad=20):
    """Crop trailing uniform-background rows (chromium viewport is taller than content)."""
    import io as _io
    from PIL import Image
    try:
        im = Image.open(_io.BytesIO(data)).convert("RGB")
    except Exception:
        return data
    w, h = im.size
    px = im.load()
    bottom = None
    for y in range(h - 1, -1, -1):
        for x in range(0, w, 3):
            if px[x, y] != bg:
                bottom = y
                break
        if bottom is not None:
            break
    if bottom is None:
        return data
    bottom = min(h, bottom + pad)
    if bottom >= h - 2:
        return data
    im2 = im.crop((0, 0, w, bottom))
    buf = _io.BytesIO()
    im2.save(buf, format="PNG")
    return buf.getvalue()


def render_png(svg_html, height=2400):
    browser = os.getenv("CHROME_BIN") or shutil.which("chromium") or shutil.which("google-chrome")
    if not browser:
        raise DataError("Chrome is required to render chart images")
    with tempfile.TemporaryDirectory(prefix="screener-chart-") as d:
        root = Path(d)
        html_file = root / "chart.html"
        html_file.write_text(
            '<!doctype html><meta charset="utf-8"><style>' + CSS + "</style>" + svg_html,
            encoding="utf-8",
        )
        image = root / "chart.png"
        cmd = [
            browser, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", "--force-device-scale-factor=1", "--timeout=5000",
            "--disable-background-networking", f"--window-size=1200,{height}",
            "--user-data-dir=" + str(root / "profile"), "--screenshot=" + str(image),
            html_file.as_uri(),
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        data = image.read_bytes()
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82"):
            return _trim(data)
        raise DataError("Chrome did not produce a complete PNG")


def crypto_bars_1h(symbol, limit=80):
    rows = get_json(BINANCE + "/klines", {"symbol": symbol, "interval": "1h", "limit": limit})
    return [
        dict(t=int(r[0]) / 1000, o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[5]))
        for r in rows
    ]


def stock_bars_1h(symbol, headers, now, days=12):
    payload = get_json(
        "https://data.alpaca.markets/v2/stocks/bars",
        dict(symbols=symbol, timeframe="1Hour", start=iso(now - days * 86400), end=iso(now),
             adjustment="split", feed="iex", limit=10000, sort="asc"),
        headers,
    )
    rows = payload.get("bars", {}).get(symbol, [])
    return [
        dict(t=timestamp(r["t"]), o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]), v=float(r["v"]))
        for r in rows
    ]


def metal_bars_1h(symbol, headers, limit=80):
    instrument = symbol[:3] + "_" + symbol[3:]
    raw = get_json(
        OANDA_BASE + "/v3/instruments/" + instrument + "/candles",
        {"granularity": "H1", "count": limit, "price": "M"},
        headers,
    )
    return [
        dict(t=timestamp(c["time"]), o=float(c["mid"]["o"]), h=float(c["mid"]["h"]),
             l=float(c["mid"]["l"]), c=float(c["mid"]["c"]), v=float(c["volume"]))
        for c in raw["candles"]
    ]
