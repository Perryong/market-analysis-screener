"""Local comparisons of immutable history with the verified reporting renderer."""
import html
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .reporting import verified_read, gap_events, evidence_signature
from .shared import DataError

SGT = timezone(timedelta(hours=8))


def archived_snapshot(path):
    text = path.read_text(encoding='utf-8')
    sidecar = path.with_suffix('.json')
    if sidecar.exists():
        snapshot = json.loads(sidecar.read_text(encoding='utf-8'))
        if snapshot.get('schema_version') != 1:
            raise DataError(f'Unsupported evidence schema: {sidecar}')
        return snapshot, text

    def match(pattern):
        found = re.search(pattern, text)
        if not found:
            raise DataError(f'Missing archived field in {path}: {pattern}')
        return found

    def numeric(value):
        return float(value.replace(',', '')) if value != '—' else None

    signal = match(r'\*\*Signal:\*\* (.+)').group(1)
    parts = signal.split(' · ')
    status = parts[1]
    side_match = re.search(r'\b(BUY|SELL)\b', signal)
    side = {'BUY': 'LONG', 'SELL': 'SHORT'}.get(side_match.group(1)) if side_match else None
    pivots = match(r'- Upper pivot: ([\d,.]+|—) · Lower pivot: ([\d,.]+|—)')
    result = dict(status=status, side=side, upper=numeric(pivots.group(1)),
                  lower=numeric(pivots.group(2)), checked_at=None, reasons=[])
    # Only structured fields are evidence. Never recover levels from old AI prose.
    for name in ('entry', 'stop', 'target'):
        m = re.search(r'\b'+name+r' ([\d,.]+|—)', signal)
        if m:
            result['plan_'+name] = numeric(m.group(1))
    direction = match(r'\*\*(?:Daily|4H) direction:\*\* (Bullish|Bearish)').group(1)
    combined = match(r'COMBINED \((1D|4H)\+1H\): (BUY|SELL|HOLD)')
    last = match(r'- Last completed: 1H (.+?) \+08 · (?:1D|4H) (.+?) \+08')
    ends = [datetime.strptime(last.group(i), '%Y-%m-%d %H:%M:%S').replace(tzinfo=SGT).timestamp() for i in (1, 2)]
    gaps = [(typ, numeric(low), numeric(high)) for typ, low, high in
            re.findall(r'(bull|bear): ([\d,.]+)–([\d,.]+)', match(r'- Open FVG: (.+)').group(1))]
    price = numeric(match(r'- Price: ([\d,.]+)').group(1))
    observed = datetime.strptime(path.stem, '%Y-%m-%d_%H%M%S').replace(tzinfo=SGT).timestamp()
    snapshot = dict(schema_version=1, observed_at=observed, symbol=path.parent.name,
                    market='metals' if path.parent.name in ('XAUUSD', 'XAGUSD') else
                           ('crypto' if path.parent.name.endswith('USDT') else 'stocks'),
                    full_precision=False, source='legacy Markdown archive', market_open=None,
                    price=dict(value=price, time=None, kind='archive'), quote={}, result=result,
                    candle_ends=dict(hourly=ends[0], setup=ends[1]), hourly=[], setup=[],
                    tf=combined.group(1), combined=combined.group(2), direction=direction,
                    trend_up='1H trend: ↑' in text, fvgs_near=gaps)
    return snapshot, text


def preview_history(root, out, symbols=None, only=None):
    """Write a local HTML comparison and JSON evidence; no model, market, send or git calls."""
    root, out = Path(root).resolve(), Path(out).resolve()
    if out == root or root in out.parents:
        raise DataError('Preview output must be outside the source history directory')
    rows, previous = [], {}
    for path in sorted(root.glob('*/*.md')):
        if symbols and path.parent.name not in symbols:
            continue
        snapshot, text = archived_snapshot(path)
        if only and snapshot['market'] != only:
            continue
        key = (snapshot['market'], snapshot['symbol'])
        prior = previous.get(key)
        evidence = {k: snapshot[k] for k in ('price', 'candle_ends', 'fvgs_near')}
        signature = evidence_signature(snapshot)
        snapshot['previous'] = prior['evidence'] if prior else None
        snapshot['unchanged'] = bool(prior and prior['signature'] == signature)
        snapshot['gap_events'] = gap_events(snapshot)
        read, mode = verified_read(snapshot)
        old = re.search(r'## (?:AI read|Verified read)\s*\n(.*?)(?:\n\n|\Z)', text, re.S)
        rows.append(dict(source=str(path), snapshot=snapshot, original=old.group(1).strip() if old else 'Unavailable',
                         verified=read, read_mode=mode))
        previous[key] = dict(evidence=evidence, signature=signature)
    if not rows:
        raise DataError('No history reports found for this preview')
    out.mkdir(parents=True, exist_ok=True)
    (out/'evidence.json').write_text(json.dumps(rows, indent=2, allow_nan=False), encoding='utf-8')
    esc = html.escape
    cards = []
    for row in sorted(rows, key=lambda r: r['snapshot']['observed_at'], reverse=True):
        snap = row['snapshot']
        title = f"{snap['symbol']} · {datetime.fromtimestamp(snap['observed_at'], SGT):%d %b %Y %H:%M SGT}"
        cards.append(f'<article data-symbol="{esc(snap["symbol"])}"><h2>{esc(title)}</h2>'
                     f'<p class="status">{esc(snap["result"].get("status", "UNKNOWN"))} · {esc(snap["combined"])} bias</p>'
                     f'<div class="comparison"><section><h3>Original AI read — unvalidated</h3><p>{esc(row["original"])}</p></section>'
                     f'<section><h3>New verified summary</h3><p>{esc(row["verified"])}</p></section></div></article>')
    options = ''.join(f'<option>{esc(s)}</option>' for s in sorted({r['snapshot']['symbol'] for r in rows}))
    document = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reporting accuracy review</title><style>
body{font:16px/1.6 system-ui,sans-serif;max-width:1280px;margin:32px auto;padding:0 24px;color:#172b3a;background:#f5f7fa}
h1,h2,h3{line-height:1.25}h2{font-size:21px}h3{font-size:17px}article{background:white;border:1px solid #ccd5df;padding:24px;margin:24px 0;border-radius:8px}
.comparison{display:grid;grid-template-columns:1fr 1fr;gap:32px}.status{font-weight:650;color:#305679}select{font:inherit;padding:8px}
section p{white-space:pre-wrap}a{color:#145aa0}@media(max-width:800px){.comparison{grid-template-columns:1fr}}
</style><h1>Reporting accuracy review</h1>
<p>Local preview · Original history preserved · No messages sent · No commits</p>
<p>The right column uses the production verified-fact renderer, without an AI call. Legacy reports contain rounded prices and incomplete evidence;
this preview does not reconstruct missing precision, recompute the historical strategy, or measure profitability.</p>
'''+f'<p>{len(rows)} reports · <a href="evidence.json">Recorded evidence and comparisons</a></p>'
    document += '<label for="symbol">Asset </label><select id="symbol"><option value="">All assets</option>'+options+'</select>'+''.join(cards)
    document += '''<script>document.querySelector('#symbol').addEventListener('change',event=>{
document.querySelectorAll('article').forEach(card=>card.hidden=!!event.target.value&&card.dataset.symbol!==event.target.value);
});</script></html>'''
    (out/'index.html').write_text(document, encoding='utf-8')
    return len(rows)
