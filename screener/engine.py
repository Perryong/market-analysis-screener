"""Pure, chronological screening. The caller verifies session coverage/freshness."""
from copy import deepcopy
from statistics import mean, median
import math

from .shared import DataError, fingerprint

DEFAULTS = dict(lookback=20, volume_ratio=1.5, breakout_atr=.1,
                compression_ratio=.8, developing_atr=.5, retest_band_atr=.2,
                retest_hours=12, stop_buffer_atr=.1, max_entry_atr=.5,
                min_rr=2., round_trip_cost_bps=None, slippage_bps=5.)
ACTIVE = ('CONFIRMED', 'RETESTED', 'ENTRY_ELIGIBLE')


def validate(bars, now):
    previous = None
    for b in bars:
        if any(isinstance(b[k], bool) or not isinstance(b[k], (int, float)) or
               not math.isfinite(b[k]) for k in ('start', 'end', 'open', 'high', 'low', 'close', 'volume')):
            raise DataError('Non-finite candle fields')
        if b['end'] > now:
            raise DataError('Incomplete/future candle')
        if b['start'] >= b['end'] or (previous is not None and b['start'] < previous):
            raise DataError('Duplicate, overlapping or unordered candles')
        if not 0 < b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high'] or b['volume'] < 0:
            raise DataError('Invalid OHLCV')
        previous = b['end']


def atr(bars, n=14):
    # Session gaps are validated against the exchange calendar by feeds.py.
    if len(bars) < n+1:
        return None
    return mean(max(b['high']-b['low'], abs(b['high']-a['close']), abs(b['low']-a['close']))
                for a, b in zip(bars[-n-1:-1], bars[-n:]))


def regime(bars):
    if len(bars) < 205:
        return 'UNKNOWN'
    values = [b['close'] for b in bars]
    fast, slow, prior = mean(values[-50:]), mean(values[-200:]), mean(values[-55:-5])
    if values[-1] > fast > slow and fast > prior:
        return 'BULLISH'
    if values[-1] < fast < slow and fast < prior:
        return 'BEARISH'
    return 'NEUTRAL'


def features(bars, settings):
    n = settings['lookback']
    if len(bars) < max(n+1, 22):
        raise DataError('Insufficient setup history')
    prior, current = bars[-n-1:-1], bars[-1]
    volatility = atr(bars[:-1])
    volume = median(b['volume'] for b in prior)
    if not volatility or not volume:
        raise DataError('Unavailable volatility or volume baseline')
    upper, lower = max(b['high'] for b in prior), min(b['low'] for b in prior)
    return dict(upper=upper, lower=lower, atr=volatility,
                volume_ratio=current['volume']/volume,
                compression=atr(bars, 5)/atr(bars, 20), close=current['close'])


def entry_check(state, bundle, settings, now):
    q = bundle.get('quote') or {}
    entry, qt = q.get('price'), q.get('time')
    reasons, metrics = [], {}
    if (isinstance(entry,bool) or not isinstance(entry, (int, float)) or not math.isfinite(entry) or entry <= 0 or
            isinstance(qt,bool) or not isinstance(qt, (int, float)) or not math.isfinite(qt) or
            not 0 <= now-qt <= 300 or qt < state['retest_end']):
        return {}, ['QUOTE_UNAVAILABLE']
    if now-state['retest_end'] > 5400 or not bundle.get('market_open', True):
        reasons.append('ENTRY_EXPIRED_OR_MARKET_CLOSED')
    direction = 1 if state['side'] == 'LONG' else -1
    entry *= 1 + direction*settings['slippage_bps']/10000
    risk = direction*(entry-state['stop'])
    reward = direction*(state['target']-entry)
    distance = abs(entry-state['level']) / state['entry_atr']
    if direction*(entry-state['level']) <= 0:
        reasons.append('WRONG_SIDE')
    if distance > settings['max_entry_atr']:
        reasons.append('EXTENDED')
    if risk <= 0 or state['stop'] <= 0:
        reasons.append('INVALID_STOP')
    costs = settings['round_trip_cost_bps']
    metrics.update(entry=entry, stop=state['stop'], target=state['target'], distance_atr=distance)
    if costs is None:
        reasons.append('COSTS_UNKNOWN')
    elif risk > 0:
        # Entry slippage already applied; reserve exit slippage in both scenarios.
        cost = entry*(costs+settings['slippage_bps'])/10000
        metrics['net_rr'] = (reward-cost)/(risk+cost)
        if metrics['net_rr'] < settings['min_rr']:
            reasons.append('INSUFFICIENT_REWARD')
    return metrics, reasons


def plan(result, state, settings):
    """Conditional levels for display only; never used for eligibility, alerts or paper trades.
    Entry assumes a retest at the trigger; stop sits past the deepest allowed retest touch."""
    side = result.get('side')
    if side not in ('LONG', 'SHORT'):
        return {}
    sign = 1 if side == 'LONG' else -1
    level = result.get('level', result['upper'] if sign == 1 else result['lower'])
    target = result.get('target', level+sign*(result['upper']-result['lower']))
    buffer = (settings['retest_band_atr']+settings['stop_buffer_atr'])*state.get('setup_atr', result['atr'])
    stop = state.get('stop', level-sign*buffer)
    risk = sign*(level-stop)
    return dict(plan_level=level, plan_entry=level, plan_stop=stop, plan_target=target,
                plan_rr=sign*(target-level)/risk if risk > 0 else None)


def evaluate(bundle, saved, settings, now):
    setup, hourly = bundle['setup'], bundle['hourly']
    for bars in (setup, hourly):
        validate(bars, now)
        if not bars:
            raise DataError('Missing candles')
    current = features(setup, settings)
    state = deepcopy(saved) if saved else dict(status='WATCHING', setup_end=setup[-1]['end'],
                                             hour_end=hourly[-1]['end'])
    if saved:
        for name, bars, key in (('setup', setup, 'setup_end'), ('hour', hourly, 'hour_end')):
            anchor = next((b for b in bars if b['end'] == saved[key]), None)
            if anchor is None or fingerprint(anchor) != saved[name+'_anchor']:
                raise DataError('Missing/revised anchor; explicit rebaseline required')
        if saved['status'] in ACTIVE:
            for name,bars in (('setup_evidence',setup),('hour_evidence',hourly)):
                indexed = {b['end']:fingerprint(b) for b in bars}
                if any(indexed.get(t) != digest for t,digest in saved.get(name,[])):
                    raise DataError('Missing/revised setup evidence; explicit rebaseline required')
    events = []

    def change(status, when):
        if state['status'] != status:
            state['status'] = status
            events.append(dict(status=status, at=when, side=state.get('side'),
                               trigger=state.get('trigger'), regime=bundle['regime']))

    timeline = [(b['end'], 0, i, b) for i,b in enumerate(setup) if b['end'] > state['setup_end']]
    timeline += [(b['end'], 1, i, b) for i,b in enumerate(hourly) if b['end'] > state['hour_end']]
    for when, kind, i, b in sorted(timeline):
        if kind == 0:
            f = features(setup[:i+1], settings)
            side = ('LONG' if b['close'] > f['upper'] + settings['breakout_atr']*f['atr'] else
                    'SHORT' if b['close'] < f['lower'] - settings['breakout_atr']*f['atr'] else None)
            prior_close = setup[i-1]['close']
            crossed = side and (prior_close <= f['upper'] if side == 'LONG' else prior_close >= f['lower'])
            if crossed and f['volume_ratio'] >= settings['volume_ratio'] and state['status'] not in ACTIVE:
                level = f['upper'] if side == 'LONG' else f['lower']
                state.update(side=side, level=level, target=level+(f['upper']-f['lower'])*(1 if side=='LONG' else -1),
                             upper=f['upper'],lower=f['lower'],
                             trigger=when, setup_atr=f['atr'], age=0, volume_ratio=f['volume_ratio'],
                             setup_evidence=[(v['end'],fingerprint(v)) for v in setup[max(0,i-settings['lookback']):i+1]],
                             hour_evidence=[])
                state.pop('retest_end', None)
                change('CONFIRMED', when)
            continue
        if state['status'] not in ACTIVE or b['start'] < state['trigger']:
            continue
        sign = 1 if state['side'] == 'LONG' else -1
        state.setdefault('hour_evidence',[]).append((b['end'],fingerprint(b)))
        state['age'] += 1
        if sign*(b['close']-state['level']) <= 0:
            change('INVALIDATED', when)
        elif (b['high'] >= state['target'] if sign==1 else b['low'] <= state['target']):
            change('MISSED', when)
        elif state['age'] > settings['retest_hours']:
            change('EXPIRED', when)
        elif state['status'] == 'CONFIRMED':
            band = settings['retest_band_atr']*state['setup_atr']
            touched = b['low'] <= state['level']+band and b['high'] >= state['level']-band
            if sign*(b['open']-state['level']) > 0 and touched:
                vol = atr(hourly[:i+1])
                if vol:
                    stop = b['low']-settings['stop_buffer_atr']*vol if sign==1 else b['high']+settings['stop_buffer_atr']*vol
                    state.update(stop=stop, entry_atr=vol, retest_end=when)
                    change('RETESTED', when)
    metrics, reasons = {}, []
    if state['status'] in ('RETESTED', 'ENTRY_ELIGIBLE'):
        metrics, reasons = entry_check(state, bundle, settings, now)
        change('RETESTED' if reasons else 'ENTRY_ELIGIBLE', now)
    if state['status'] in ('WATCHING', 'DEVELOPING'):
        distance = min(abs(current['close']-current['upper']), abs(current['close']-current['lower']))/current['atr']
        developing = current['compression'] <= settings['compression_ratio'] and distance <= settings['developing_atr']
        change('DEVELOPING' if developing else 'WATCHING', setup[-1]['end'])
        state['side'] = 'LONG' if abs(current['close']-current['upper']) < abs(current['close']-current['lower']) else 'SHORT'
    aligned = (state.get('side'), bundle['regime']) in (('LONG','BULLISH'), ('SHORT','BEARISH'))
    rank = round(min(current['volume_ratio'], 3)*10 + max(0, 1-current['compression'])*30 + (10 if aligned else 0), 1)
    result = dict(symbol=bundle['symbol'], market=bundle['market'], regime=bundle['regime'],
                  status=state['status'], side=state.get('side'), score=rank, checked_at=now,
                  setup_close=setup[-1]['end'], hourly_close=hourly[-1]['end'],
                  reasons=reasons, **current, **metrics)
    for k in ('upper','lower','level', 'trigger', 'target', 'retest_end'):
        if k in state:
            result[k] = state[k]
    result.update(plan(result, state, settings))
    state.update(setup_end=setup[-1]['end'], hour_end=hourly[-1]['end'],
                 setup_anchor=fingerprint(setup[-1]), hour_anchor=fingerprint(hourly[-1]))
    return result, state, events


def paper_step(position, bars, sessions=()):
    p = deepcopy(position)
    if p.get('status', 'OPEN') != 'OPEN':
        return p
    for b in bars:
        if b['end'] <= max(p['entry_at'], p['last_end']):
            continue
        gap_start = max(p['entry_at'],p['last_end'])
        missing = b['start'] > gap_start
        if p.get('market') == 'stocks':
            missing = (not sessions or any(max(gap_start,op) < min(b['start'],cl) for op,cl in sessions))
        if missing:
            p.update(status='UNSCORABLE',exit_reason='MISSING_EXECUTION_BARS')
            return p
        stop, target = b['low'] <= p['stop'], b['high'] >= p['target']
        if b['start'] < p['entry_at'] and (stop or target):
            p.update(status='UNSCORABLE', exit_reason='ENTRY_BAR_ORDER_UNKNOWN')
            return p
        p['last_end'] = b['end']
        if stop or target:
            exit_price = min(b['open'], p['stop']) if stop else p['target']
            exit_price *= 1-p['slippage_bps']/10000
            p.update(status='CLOSED', exit=exit_price, exit_at=b['end'], exit_reason='STOP' if stop else 'TARGET',
                     pnl=p['quantity']*(exit_price-p['entry']-p['entry']*p['cost_bps']/10000))
            return p
    return p
