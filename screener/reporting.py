"""Verified reporting facts. This module never changes strategy state or eligibility."""
from decimal import Decimal
import math
from .shared import fingerprint


def evidence_signature(snapshot):
    # Quote/scan clock advances alone are not independent market confirmation.
    return fingerprint([snapshot['price']['value'], snapshot.get('setup'), snapshot.get('hourly'),
                        snapshot['candle_ends'], snapshot['fvgs_near'], snapshot['combined'],
                        snapshot['direction'], snapshot['trend_up'], snapshot.get('market_open'),
                        {k: snapshot['result'].get(k) for k in
                         ('status', 'side', 'upper', 'lower', 'entry', 'stop', 'target',
                          'plan_entry', 'plan_stop', 'plan_target', 'reasons', 'regime')}])


def number(value):
    if value is None:
        return '—'
    return format(Decimal(str(value)), ',f')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def evidence_notes(snapshot):
    now = snapshot['observed_at']
    notes = []
    if not snapshot.get('full_precision', True):
        notes.append('Legacy archive: rounded values; quote timestamps and full candle evidence unavailable')
    if snapshot.get('market_open') is False:
        notes.append('Market closed; this is a historical observation')
    elif snapshot.get('market_open') is None:
        notes.append('Market session status unavailable')
    price = snapshot['price']
    qt = price.get('time')
    if not finite(qt):
        notes.append('Price/quote timestamp unavailable')
    elif not 0 <= now-qt <= 300:
        notes.append('Price/quote is stale or future-dated; not a current execution quote')
    if price.get('kind') != 'quote':
        notes.append('Price is a recorded candle close or archive value, not a live execution quote')
    hourly = snapshot['candle_ends'].get('hourly')
    if not finite(hourly) or not 0 <= now-hourly <= 7200:
        notes.append('Hourly candles are old, missing or future-dated; no fresh confirmation')
    checked = snapshot['result'].get('checked_at')
    if not finite(checked) or not 0 <= now-checked <= 300:
        notes.append('Strategy assessment timestamp is old or unavailable')
    for name, key in [('setup', 'setup_close'), ('hourly', 'hourly_close')]:
        end = snapshot['candle_ends'].get(name)
        if finite(end) and end > now:
            notes.append(f'{name} candle is future-dated')
        if snapshot['result'].get(key) != end:
            notes.append(f'{name} candles are not matched to the saved strategy assessment')
    return notes


def gap_events(snapshot):
    """A missing gap is not a fill. Only report boundary touches evidenced by later bars."""
    prev = snapshot.get('previous') or {}
    last = (prev.get('candle_ends') or {}).get('hourly')
    if not finite(last):
        return []
    events = []
    current = [tuple(f) for f in snapshot['fvgs_near']]
    for typ, low, high in prev.get('fvgs_near', []):
        if (typ, low, high) in current:
            continue
        touched = next((b for b in snapshot.get('hourly', [])
                        if b['start'] >= last and b['end'] <= snapshot['observed_at']
                        and (b['low'] <= low <= b['high'] or b['low'] <= high <= b['high'])), None)
        event = dict(type='boundary_touched' if touched else 'no_longer_listed',
                     gap=[typ, low, high])
        if touched:
            event['bar'] = touched
        events.append(event)
    return events


def fact_catalog(snapshot):
    r = snapshot['result']
    status = r.get('status') or 'UNKNOWN'
    notes = evidence_notes(snapshot)
    executable = [r.get(k) for k in ('entry', 'stop', 'target')]
    valid_levels = all(finite(v) and v > 0 for v in executable)
    if valid_levels:
        entry, stop, target = executable
        valid_levels = ((r.get('side') == 'LONG' and stop < entry < target) or
                        (r.get('side') == 'SHORT' and target < entry < stop))
    verified = (status == 'ENTRY_ELIGIBLE' and not r.get('reasons') and not notes
                and valid_levels and snapshot.get('market_open') is True)
    if verified:
        state = f'Strategy: {status} ({r.get("side")}). The engine marks this entry eligible; no fill or position is implied.'
    elif status == 'ENTRY_ELIGIBLE':
        state = f'Strategy: {status} recorded, but these inputs cannot verify a current eligible entry. No fill or position is implied.'
    else:
        state = f'Strategy: {status}. No eligible entry.'
    if r.get('reasons'):
        state += ' Engine blockers: ' + ', '.join(r['reasons']) + '.'
    mandatory = dict(status=state,
                     bias=f'Directional confluence: {snapshot["combined"]}; {snapshot["tf"]} direction {snapshot["direction"]}, '
                          f'1H trend {"up" if snapshot["trend_up"] else "down"}. Directional bias does not activate a trade.',
                     evidence=('; '.join(notes) + '.') if notes else 'Quote, candle and strategy timestamps are current and matched.')
    if snapshot.get('unchanged'):
        mandatory['repeat'] = 'No new market evidence since the previous report; this is not an additional confirmation.'
    plan = [r.get('plan_'+k) for k in ('entry', 'stop', 'target')]
    if any(v is not None for v in plan) and status != 'DATA_UNAVAILABLE':
        mandatory['plan'] = (f'Conditional plan only: entry {number(plan[0])}, stop {number(plan[1])}, target {number(plan[2])}. '
                             'These levels do not establish an entry, stop-out or target hit.')
    if status == 'ENTRY_ELIGIBLE' and valid_levels:
        mandatory['execution'] = ('Engine-assessed levels: entry '+number(executable[0])+', stop '+number(executable[1])+
                                  ', target '+number(executable[2])+'.')
    for i, ev in enumerate(gap_events(snapshot)):
        typ, low, high = ev['gap']
        label = f'{typ} gap {number(low)}–{number(high)}'
        if ev['type'] == 'boundary_touched':
            mandatory[f'event:{i}'] = f'{label}: boundary touched by a subsequent recorded candle; this does not establish a full fill.'
        else:
            mandatory[f'event:{i}'] = f'{label}: no longer listed; removal alone is not evidence of a fill.'
    price = snapshot['price']['value']
    optional = dict(price=f'Reference price: {number(price)}. Upper pivot {number(r.get("upper"))}; lower pivot {number(r.get("lower"))}.')
    for i, (typ, low, high) in enumerate(snapshot['fvgs_near']):
        relation = 'below' if price < low else ('above' if price > high else 'inside')
        optional[f'gap:{i}'] = f'Price is {relation} the listed {typ} gap {number(low)}–{number(high)}.'
    previous_price = ((snapshot.get('previous') or {}).get('price') or {}).get('value')
    if finite(previous_price):
        relation = 'higher than' if price > previous_price else ('lower than' if price < previous_price else 'unchanged from')
        optional['change'] = f'Reference price is {relation} the previous {number(previous_price)}; no intervening price path is inferred.'
    return mandatory, optional


def verified_read(snapshot, selection=None):
    """Only allow catalogue IDs; no model-authored prose reaches a user or the cache."""
    mandatory, optional = fact_catalog(snapshot)
    chosen = list(optional)[:5]
    mode = 'verified summary (no AI selection)'
    if selection is not None:
        ids = selection.get('fact_ids') if isinstance(selection, dict) else None
        valid = (isinstance(selection, dict) and set(selection) == {'fact_ids'} and
                 isinstance(ids, list) and 1 <= len(ids) <= 5 and
                 all(isinstance(i, str) and i in optional for i in ids))
        if valid and len(set(ids)) == len(ids):
            chosen, mode = ids, 'AI-selected verified facts'
        else:
            mode = 'fallback: invalid AI selection'
    return ' '.join([*mandatory.values(), *(optional[i] for i in chosen)]), mode
