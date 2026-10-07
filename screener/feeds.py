"""Read-only market data. All cached data remains attributable to its feed."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os

from .shared import get_json, iso, timestamp
from .shared import DataError
from .engine import validate, regime

NY = ZoneInfo('America/New_York')
BINANCE = 'https://data-api.binance.vision/api/v3'


def aggregate(bars):
    return dict(start=bars[0]['start'], end=bars[-1]['end'], open=bars[0]['open'],
                high=max(b['high'] for b in bars), low=min(b['low'] for b in bars),
                close=bars[-1]['close'], volume=sum(b['volume'] for b in bars))


def stock_bars(rows, sessions, now):
    validate(rows, now)
    if not rows:
        raise DataError('Missing stock history')
    by_start = {b['start']:b for b in rows}
    daily, hourly = [], []
    for op, cl in sessions:
        if cl <= rows[0]['start'] or op > now:
            continue
        parts = []
        for start in range(int(op), int(cl), 1800):
            if start+1800 > now:
                break
            b = by_start.get(start)
            if b is None or b['end'] != start+1800:
                raise DataError('Missing regular-session 30-minute candle')
            parts.append(b)
            if len(parts) % 2 == 0:
                hourly.append(aggregate(parts[-2:]))
        if cl <= now and parts:
            daily.append(aggregate(parts))
    return daily, hourly


def crypto_bars(rows, duration, now):
    result = []
    for r in rows:
        start = int(r[0])/1000
        if int(r[6])+1 != (start+duration)*1000 or start % duration:
            raise DataError('Invalid crypto candle interval')
        if start+duration <= now:
            result.append(dict(start=start, end=start+duration,
                               **dict(zip(('open','high','low','close','volume'), map(float,r[1:6])))))
    validate(result, now)
    if not result or any(a['end'] != b['start'] for a,b in zip(result,result[1:])):
        raise DataError('Missing crypto candles')
    if result[-1]['end'] != int(now)//duration*duration:
        raise DataError('Latest completed crypto candle unavailable')
    return result


def slot(market, now, sessions, active=False):
    if market == 'crypto':
        interval, grace = (300, 0) if active else (3600, 180)
        return (int(now)-grace)//interval*interval+grace
    today = datetime.fromtimestamp(now, NY).date()
    for op, cl in sessions:
        if datetime.fromtimestamp(op, NY).date() != today:
            continue
        slots = [op-900, cl+600]
        slots += list(range(int(op)+3720, int(cl)+1, 3600))
        if active and op <= now < cl:
            slots.append(int(now)//300*300)
        return max((s for s in slots if s <= now), default=None)
    return None


class MarketData:
    def __init__(self, config, cache, now):
        self.config, self.cache, self.now = config, cache, now
        self.sessions = []
        self.headers = {}

    def calendar(self):
        if self.config['stocks'].get('provider', 'alpaca') == 'yfinance':
            from . import yahoo
            self.sessions = yahoo.calendar(self.now)
            return self.sessions
        key, secret = os.getenv('APCA_API_KEY_ID'), os.getenv('APCA_API_SECRET_KEY')
        if not key or not secret:
            raise DataError('Set APCA_API_KEY_ID and APCA_API_SECRET_KEY for stock data')
        self.headers = {'APCA-API-KEY-ID':key, 'APCA-API-SECRET-KEY':secret}
        day = datetime.fromtimestamp(self.now, NY).date().isoformat()
        saved = self.cache.get('calendar', {})
        if saved.get('date') != day:
            base = 'https://paper-api.alpaca.markets' if self.config['stocks']['environment']=='paper' else 'https://api.alpaca.markets'
            rows = get_json(base+'/v2/calendar', {'start':iso(self.now-430*86400)[:10],
                                                'end':iso(self.now+14*86400)[:10]}, self.headers)
            sessions = []
            for r in rows:
                op, cl = (datetime.fromisoformat(r['date']+'T'+r[k]).replace(tzinfo=NY).timestamp() for k in ('open','close'))
                if not 0 < cl-op <= 86400:
                    raise DataError('Invalid exchange session')
                sessions.append([op,cl])
            saved = dict(date=day, sessions=sorted(sessions))
            self.cache['calendar'] = saved
        self.sessions = saved['sessions']
        if not self.sessions:
            raise DataError('Empty exchange calendar')
        return self.sessions

    def stock(self, symbol):
        if not self.sessions:
            self.calendar()
        if self.config['stocks'].get('provider', 'alpaca') == 'yfinance':
            from . import yahoo
            return yahoo.stock(symbol, self.sessions, self.cache, self.now)
        feed = self.config['stocks']['feed']
        key = 'stock:'+feed+':'+symbol
        saved = self.cache.get(key, {})
        full = self.now-saved.get('full_at', 0) > 7*86400
        start = self.now-(420 if full else 3)*86400
        # SIP on free/paper accounts is 15-min delayed; end=now 403s ("subscription
        # does not permit querying recent SIP data"). Treat the delayed point as the
        # effective "data now" for the WHOLE pipeline (fetch end, bar completeness,
        # daily close) so we never anchor on a 30-min bar still consolidating.
        data_now = self.now - 900
        # Begin at a session boundary, never midway through the first day.
        start = datetime.fromtimestamp(start, NY).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        rows, token, seen = [], None, set()
        while True:
            params = dict(symbols=symbol, timeframe='30Min', start=iso(start), end=iso(data_now),
                          adjustment='split', feed=feed, limit=10000, sort='asc')
            if token:
                params['page_token'] = token
            payload = get_json('https://data.alpaca.markets/v2/stocks/bars', params, self.headers)
            rows.extend(payload.get('bars', {}).get(symbol, []))
            token = payload.get('next_page_token')
            if not token:
                break
            if token in seen or len(seen) >= 100:
                raise DataError('Invalid pagination')
            seen.add(token)
        normalized = []
        for r in rows:
            t = timestamp(r['t'])
            if t+1800 <= data_now:
                normalized.append(dict(start=t, end=t+1800,
                    **{k:float(r[v]) for k,v in zip(('open','high','low','close','volume'),('o','h','l','c','v'))}))
        retained = [] if full else [b for b in saved.get('rows', []) if b['start'] < start]
        merged = sorted(retained+normalized, key=lambda b:b['start'])
        # Validation must see duplicates, never overwrite them in a dict.
        validate(merged, data_now)
        regular = [b for b in merged if any(op <= b['start'] < cl for op,cl in self.sessions)]
        daily, hourly = stock_bars(regular, self.sessions, data_now)
        self.cache[key] = dict(rows=merged, full_at=self.now if full else saved['full_at'])
        expected = max((cl for op,cl in self.sessions if cl <= data_now), default=0)
        if not daily or daily[-1]['end'] != expected:
            raise DataError('Latest stock daily candle unavailable')
        # Quote access can differ from historical-data entitlement. Keep setups visible.
        quote, quote_error = None, None
        try:
            # SIP "latest" quotes 403 on delayed-data subscriptions; IEX quotes are
            # available and real-time on the free tier, so use IEX for the quote.
            r = get_json('https://data.alpaca.markets/v2/stocks/quotes/latest',
                         {'symbols':symbol, 'feed':'iex'}, self.headers)['quotes'][symbol]
            bid, ask = float(r['bp']), float(r['ap'])
            if not 0 < bid <= ask:
                raise DataError('Invalid stock quote')
            quote = dict(price=ask, time=timestamp(r['t']))
        except (DataError, KeyError, TypeError, ValueError):
            quote_error = 'Live quote unavailable; entries blocked'
        return dict(symbol=symbol, market='stocks', source='Alpaca '+feed+' / split adjusted / regular session',
                    setup=daily[-250:], hourly=hourly[-250:], quote=quote, quote_error=quote_error,
                    execution=regular[-500:], sessions=self.sessions,
                    market_open=any(op <= self.now < cl for op,cl in self.sessions))

    def crypto(self, symbol, daily=False):
        frames = [('1d',86400)] if daily else [('4h',14400),('1h',3600)]
        bars = []
        for interval, duration in frames:
            key = 'binance:'+symbol+':'+interval
            saved = self.cache.get(key, {})
            end = int(self.now)//duration*duration
            if saved.get('end') != end:
                raw = get_json(BINANCE+'/klines', {'symbol':symbol, 'interval':interval, 'limit':250})
                saved = dict(end=end, rows=crypto_bars(raw,duration,self.now))
                self.cache[key] = saved
            bars.append(saved['rows'])
        if daily:
            return bars[0]
        r = get_json(BINANCE+'/ticker/24hr', {'symbol':symbol})
        return dict(symbol=symbol, market='crypto', source='Binance spot / UTC',
                    setup=bars[0], hourly=bars[1], quote=dict(price=float(r['lastPrice']), time=float(r['closeTime'])/1000),
                    market_open=True)
