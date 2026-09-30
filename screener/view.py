"""Dependency-free, filterable dashboard. No provider content is executable."""
import math
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

from .engine import DEFAULTS


def e(value):
    return escape(str(value),quote=True)


def number(value):
    if value is None:
        return '—'
    # At least four significant digits for sub-unit prices such as PEPEUSDT.
    places = max(4, 3-math.floor(math.log10(abs(value)))) if value else 4
    return f'{value:,.{places}f}'.rstrip('0').rstrip('.')


def level(r, key):
    """Real level when the setup has one; otherwise the display-only plan, tagged as such."""
    if r.get(key) is not None or r.get('plan_'+key) is None:
        return number(r.get(key))
    return number(r['plan_'+key])+' <em class="plan">planned</em>'


def side(r):
    value = r.get('side')
    if value not in ('LONG', 'SHORT'):
        return '—'
    return f'<span class="side {value.lower()}">{"▲" if value == "LONG" else "▼"} {value}</span>'


BROKEN_OUT = ('CONFIRMED', 'RETESTED', 'ENTRY_ELIGIBLE', 'INVALIDATED', 'MISSED', 'EXPIRED')
RETESTED = ('RETESTED', 'ENTRY_ELIGIBLE')
ENDED = ('INVALIDATED', 'MISSED', 'EXPIRED')
MIN_TRADES = 30


def checks(r):
    """Entry conditions as (label, 'pass'|'fail'|'pending', note), from values the engine already computed."""
    reasons, status, side = set(r.get('reasons') or ()), r.get('status'), r.get('side')
    retested = status in RETESTED or r.get('retest_end') is not None
    priced = r.get('entry') is not None
    def gate(label, code, passed, note):
        return (label, 'fail' if code in reasons else 'pass' if passed else 'pending', note if code in reasons else '')
    volume = r.get('volume_ratio')
    return [
        ('Trend agrees', 'pending' if side not in ('LONG', 'SHORT') else
         'pass' if (side, r.get('regime')) in (('LONG', 'BULLISH'), ('SHORT', 'BEARISH')) else 'fail',
         f'{side or "—"} setup against a {r.get("regime", "UNKNOWN")} regime'),
        ('Breakout close', 'pass' if status in BROKEN_OUT else 'pending', ''),
        ('Volume ≥ 1.5×', 'pending' if volume is None else 'pass' if volume >= DEFAULTS['volume_ratio'] else 'fail',
         'Latest candle volume is below 1.5× its 20-candle median'),
        ('1H retest', 'pass' if retested else 'fail' if status in ENDED else 'pending',
         'Setup ended before a completed 1H retest'),
        gate('Fresh quote', 'QUOTE_UNAVAILABLE', retested and priced,
             'No executable quote within 5 minutes (Yahoo stocks never provide one)'),
        ('Not overextended', 'fail' if reasons & {'EXTENDED', 'WRONG_SIDE'} else 'pass' if priced else 'pending',
         'Quote is more than 0.5 ATR from the trigger, or on the wrong side of it'),
        gate('Costs configured', 'COSTS_UNKNOWN', r.get('net_rr') is not None,
             'Set strategy.round_trip_cost_bps in screener.json'),
        gate('R:R ≥ 2', 'INSUFFICIENT_REWARD', (r.get('net_rr') or 0) >= DEFAULTS['min_rr'],
             'Reward after costs is under twice the risk'),
    ]


def track_record(events, trades):
    """Signal funnel from recorded transitions, plus closed paper trades scored in R."""
    funnel = {}
    for ev in events:
        if ev.get('status') in BROKEN_OUT:
            funnel.setdefault(ev['market'], {}).setdefault(ev['status'], set()).add((ev['symbol'], ev.get('trigger')))
    funnel = {m: {s: len(v) for s, v in states.items()} for m, states in funnel.items()}
    closed = sorted((t for t in trades if t.get('status') == 'CLOSED'), key=lambda t: t.get('exit_at') or 0)
    rs = [t['pnl']/(t['quantity']*(t['entry']-t['stop'])) for t in closed]
    peak = equity = drawdown = 0
    for x in rs:
        equity += x
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    pnl = {}
    for t in closed:
        pnl[t['currency']] = pnl.get(t['currency'], 0)+t['pnl']
    wins = sum(x > 0 for x in rs)
    return dict(funnel=funnel, closed=len(rs), wins=wins, pnl=pnl, max_drawdown_r=drawdown,
                avg_r=sum(rs)/len(rs) if rs else None,
                win_rate=wins/len(rs) if len(rs) >= MIN_TRADES else None)


ICON = {'pass': '✅', 'fail': '❌', 'pending': '○'}


def checklist(items):
    return '<ul class="checks">'+''.join(
        f'<li class="check {state}">{ICON[state]} {e(label)}{" — "+e(note) if state == "fail" and note else ""}</li>'
        for label, state, note in items)+'</ul>'


def record_panel(record):
    labels = (('CONFIRMED','Confirmed'),('RETESTED','Retested'),('ENTRY_ELIGIBLE','Entry eligible'),
              ('INVALIDATED','Invalidated'),('MISSED','Missed target'),('EXPIRED','Expired'))
    rows = ''.join(f'<tr><td>{e(m)}</td>'+''.join(f'<td>{f.get(k,0)}</td>' for k,_ in labels)+'</tr>'
                   for m,f in sorted(record['funnel'].items()))
    funnel = (f'<div class="tablewrap"><table><thead><tr><th>Market</th>{"".join("<th>"+l+"</th>" for _,l in labels)}</tr></thead>'
              f'<tbody>{rows or "<tr><td colspan=7 class=empty>No breakouts recorded yet.</td></tr>"}</tbody></table></div>')
    n = record['closed']
    if record['win_rate'] is None:
        verdict = f'Not enough history to judge — {n} of {MIN_TRADES} trades'
    else:
        verdict = f'Win rate {record["win_rate"]:.0%} over {n} closed trades'
    pnl = ', '.join(f'{number(v)} {e(c)}' for c,v in sorted(record['pnl'].items())) or '—'
    stats = (f'<section class="stats"><div class="stat"><strong>{n}</strong><span>Closed paper trades</span></div>'
             f'<div class="stat"><strong>{number(record["avg_r"])}</strong><span>Average R</span></div>'
             f'<div class="stat"><strong>{number(record["max_drawdown_r"]) if n else "—"}</strong><span>Max drawdown (R)</span></div>'
             f'<div class="stat"><strong>{pnl}</strong><span>Net P&amp;L</span></div></section>')
    return (f'<h2>Track record</h2><p class="verdict">{verdict}. Signals are counted once per breakout; '
            'results are simulated paper trades, not fills.</p>'+funnel+stats)


def stamp(value):
    return '—' if value is None else datetime.fromtimestamp(value,timezone.utc).strftime('%d %b %H:%M UTC')


def chart(r):
    bars = r.get('candles',[])
    if not bars:
        return ''
    levels = [(k,r[k]) for k in ('upper','lower','level','stop','target') if r.get(k) is not None]
    low = min([b['low'] for b in bars]+[v for k,v in levels])
    high = max([b['high'] for b in bars]+[v for k,v in levels])
    span = max(high-low,high*.001)
    y = lambda v: 155-(v-low)/span*135
    step = 610/len(bars)
    items = []
    for i,b in enumerate(bars):
        x = 15+i*step
        color = '#71d9ac' if b['close']>=b['open'] else '#f39898'
        items.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y(b["high"]):.1f}" y2="{y(b["low"]):.1f}" stroke="{color}"/>'
                     f'<rect x="{x-step*.28:.1f}" y="{min(y(b["open"]),y(b["close"])):.1f}" width="{step*.56:.1f}" height="{max(1,abs(y(b["close"])-y(b["open"]))):.1f}" fill="{color}"/>')
    grouped = {}
    for k,v in levels:
        grouped.setdefault(v,[]).append(k)
    label_y = 0
    for v,names in sorted(grouped.items(),reverse=True):
        label_y = max(y(v)+4,label_y+13)
        items.append(f'<line x1="8" x2="635" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#899aa9" stroke-dasharray="4 5"/>'
                     f'<text x="640" y="{label_y:.1f}" fill="#d0d9e2" font-size="10">{e("/".join(names))} {number(v)}</text>')
    return '<svg viewBox="0 0 790 180" role="img" aria-label="Completed candles with setup levels">'+''.join(items)+'</svg>'


def render(payload):
    results = payload['results']
    mode = payload['mode']
    demo = mode=='demo'
    title = 'SYNTHETIC DEMO · no real market signals' if demo else ('HISTORICAL REPLAY · simulated next-open fills' if mode=='replay' else 'READ-ONLY RESEARCH · no broker orders')
    rows = []
    for r in results:
        state = r['status']
        tv = ('BINANCE:' if r['market']=='crypto' else '')+r['symbol']
        reasons = ', '.join(r.get('reasons',[])) or 'Conditions evaluated from completed candles'
        rr = (number(r['net_rr'])+' net R:R' if r.get('net_rr') is not None else
              number(r['plan_rr'])+' gross R:R, planned' if r.get('plan_rr') is not None else '— net R:R')
        items = checks(r)
        details = checklist(items)+(f'<p>{e(r.get("source","Data unavailable"))} · Setup close {stamp(r.get("setup_close"))} · Hourly close {stamp(r.get("hourly_close"))}</p>'
                   f'<p>{e(reasons)}. {e(r.get("paper_note",""))} {e(r.get("quote_error") or "")}</p>'
                   f'<p>Range {number(r.get("lower"))}–{number(r.get("upper"))} · ATR {number(r.get("atr"))} · '
                   f'Compression {number(r.get("compression"))} · Trigger {stamp(r.get("trigger"))}</p>'+chart(r))
        rows.append(f'''<tbody class="candidate {e((r.get('side') or '').lower())}" data-side="{e(r.get('side') or '')}" data-market="{e(r['market'])}" data-status="{e(state)}" data-regime="{e(r['regime'])}" data-symbol="{e(r['symbol'])}" data-time="{r['checked_at']}">
<tr><td><a href="https://www.tradingview.com/chart/?symbol={quote(tv)}" target="_blank" rel="noopener noreferrer">{e(r['symbol'])} ↗</a><small>{e(r['market'])}</small></td>
<td><span class="badge {e(state.lower())}">{e(state.replace('_',' '))}</span><small>{side(r)} <span class="age"></span></small><small class="checkcount">Checks {sum(s == 'pass' for _, s, _ in items)}/{len(items)}</small></td>
<td><span class="regime {e(r['regime'].lower())}">{e(r['regime'])}</span></td><td>{number(r.get('volume_ratio'))}×</td><td>{level(r,'level')}</td>
<td>{level(r,'entry')}<small class="stop">Stop {level(r,'stop')}</small></td><td><span class="target">{level(r,'target')}</span><small>{rr}</small></td>
<td>{number(r.get('score'))}</td></tr><tr class="detail"><td colspan="8"><details><summary>Evidence, levels & chart</summary>{details}</details></td></tr></tbody>''')
    trades = ''.join(f'<tr><td>{e(t["symbol"])}</td><td>{e(t["status"])}</td><td>{e(t["regime"])}</td>'
                     f'<td>{number(t["entry"])}</td><td>{number(t.get("exit"))}</td><td>{number(t.get("pnl"))} {e(t["currency"])}</td>'
                     f'<td>{e(t.get("exit_reason","Awaiting completed execution bars"))}</td></tr>' for t in payload['trades'])
    counts = {s:sum(r['status']==s for r in results) for s in ('DEVELOPING','CONFIRMED','ENTRY_ELIGIBLE','DATA_UNAVAILABLE')}
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market Watch · Swing Screener</title><style>
:root{{color-scheme:dark;--bg:#0b1118;--panel:#111b26;--line:#263543;--text:#e5edf4;--muted:#95a7b8;--green:#71d9ac;--red:#f2777a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,sans-serif}}main{{max-width:1400px;margin:auto;padding:36px 28px}}
header{{display:flex;justify-content:space-between;align-items:start;gap:20px}}h1{{font-size:36px;letter-spacing:-1px;margin:4px 0 6px}}h2{{font-size:21px;margin-top:32px}}p{{color:var(--muted)}}.eyebrow{{color:var(--green);letter-spacing:2px;font-size:11px;font-weight:700}}.stamp{{text-align:right;color:var(--muted)}}
.notice{{border:1px solid var(--line);padding:12px 16px;border-radius:8px;margin:22px 0;color:{'#f3c976' if mode!='live' else '#95a7b8'}}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.stat{{background:var(--panel);border:1px solid var(--line);padding:18px;border-radius:10px}}.stat strong{{display:block;font-size:29px}}.stat span{{color:var(--muted)}}
.filters{{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0 14px}}input,select{{background:var(--panel);border:1px solid var(--line);color:var(--text);padding:10px 12px;border-radius:6px;font:inherit}}label{{color:var(--muted);font-size:12px}}label>*{{display:block;margin-top:5px}}input:focus,select:focus,summary:focus{{outline:2px solid var(--green)}}
.tablewrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}th{{background:#14202c;color:var(--muted);text-align:left;font-size:11px;letter-spacing:1px;text-transform:uppercase}}th,td{{padding:14px 16px}}tbody.candidate{{border-top:1px solid var(--line)}}tbody.candidate:hover{{background:#101b26}}td small{{display:block;font-size:11px;color:var(--muted);margin-top:5px}}a{{color:var(--text);font-weight:650;text-decoration:none}}a:hover{{color:var(--green)}}
.badge{{font-size:10px;font-weight:700;letter-spacing:.5px;background:#263442;border-radius:4px;padding:5px 8px}}.entry_eligible{{background:#173e32;color:#8ce5be}}.confirmed,.retested,.developing{{background:#263b53;color:#b5d9ff}}.data_unavailable,.invalidated,.expired{{color:#f4b1a0}}.detail td{{padding-top:0;padding-bottom:10px}}details{{white-space:normal;color:var(--muted)}}summary{{cursor:pointer;font-size:12px}}svg{{width:100%;max-width:1000px;margin-top:12px}}.stale .badge{{background:#493429;color:#ffd1a5}}footer{{margin-top:30px;color:var(--muted);font-size:12px}}.empty{{padding:30px;color:var(--muted)}}[hidden]{{display:none!important}}.side{{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.5px;border-radius:4px;padding:2px 7px}}.side.long{{background:#123a2b;color:var(--green)}}.side.short{{background:#43191c;color:var(--red)}}
tbody.long td:first-child{{box-shadow:inset 3px 0 var(--green)}}tbody.short td:first-child{{box-shadow:inset 3px 0 var(--red)}}
.regime.bullish{{color:var(--green)}}.regime.bearish{{color:var(--red)}}.regime.neutral,.regime.unknown{{color:var(--muted)}}td small.stop,td small.stop .plan{{color:var(--red)}}.target{{color:var(--green)}}.checks{{list-style:none;padding:0;margin:10px 0;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:4px 16px}}.check.pass{{color:var(--green)}}.check.fail{{color:var(--red)}}.check.pending{{color:var(--muted)}}.verdict{{color:var(--text)}}.tablewrap+.stats{{margin-top:14px}}.plan{{font-style:normal;font-size:10px;color:var(--muted);margin-left:4px}}
@media(max-width:700px){{main{{padding:22px 14px}}header{{display:block}}.stamp{{text-align:left}}h1{{font-size:28px}}.stats{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><main><header><div><div class="eyebrow">MARKET WATCH / RESEARCH DESK</div><h1>Breakout & breakdown screener</h1><p>Daily stocks · 4H crypto · 1H retests</p></div><div class="stamp">{stamp(payload['generated_at'])}<br><a href="latest.json">Download evidence JSON ↗</a></div></header>
<div class="notice">{title}. Regime guides ranking; scores are not win probabilities. Costs must be configured before entry eligibility.</div>
{'<div class="notice">Stocks use Yahoo research candles. No verified executable quotes: stock entry eligibility and new paper entries are blocked.</div>' if any('Yahoo Finance' in r.get('source','') for r in results) else ''}
<section class="stats" aria-label="Scan summary">{''.join(f'<div class="stat"><strong>{n}</strong><span>{e(s.replace("_"," ").title())}</span></div>' for s,n in counts.items())}</section>
<div class="filters"><label>Symbol<input id="search" type="search" placeholder="Search symbol"></label>
<label>Market<select id="market"><option value="">All markets</option><option>stocks</option><option>crypto</option></select></label>
<label>State<select id="status"><option value="">All states</option>{''.join(f'<option>{e(s)}</option>' for s in sorted(set(r['status'] for r in results)))}</select></label>
<label>Regime<select id="regime"><option value="">All regimes</option><option>BULLISH</option><option>BEARISH</option><option>NEUTRAL</option><option>UNKNOWN</option></select></label>
<label>Side<select id="side"><option value="">All sides</option><option value="LONG">Long</option><option value="SHORT">Short</option></select></label></div>
<div class="tablewrap"><table><thead><tr>{''.join('<th>'+x+'</th>' for x in ('Instrument','Setup state','Market regime','Rel. volume','Trigger level','Entry / stop','Target / reward','Rank'))}</tr></thead>{''.join(rows)}</table></div>
<p id="empty" hidden>No candidates match these filters.</p>
{record_panel(track_record(payload.get('events',[]),payload['trades']))}
<h2>Paper journal</h2><p>Simulated longs only. USD and USDT budgets are separate. Unknown execution order is unscorable, never a win.</p>
<div class="tablewrap"><table><thead><tr>{''.join('<th>'+x+'</th>' for x in ('Instrument','State','Entry regime','Entry','Exit','Net result','Resolution'))}</tr></thead><tbody>{trades or '<tr><td colspan="7" class="empty">No simulated trades yet. Signals and observations are not fills.</td></tr>'}</tbody></table></div>
<footer>Breakdowns on spot instruments are informational. Check timestamps, feed coverage and costs before acting. Stale rows are flagged after 90 minutes. No orders or protective stops are placed.</footer>
</main><script>
const rows=[...document.querySelectorAll('.candidate')];
function update(){{const q=document.querySelector('#search').value.toUpperCase();let visible=0;for(const row of rows){{let show=row.dataset.symbol.toUpperCase().includes(q);for(const key of ['market','status','regime','side']){{const value=document.getElementById(key).value;show=show&&(!value||row.dataset[key]===value);}}row.hidden=!show;if(show)visible++;const stale=Date.now()/1000-Number(row.dataset.time)>5400;row.classList.toggle('stale',stale);row.querySelector('.age').textContent=stale?'· STALE — RECHECK':'';}}document.querySelector('#empty').hidden=visible>0;}}
document.querySelectorAll('input,select').forEach(el=>el.addEventListener('input',update));update();setInterval(update,30000);
</script></body></html>'''
