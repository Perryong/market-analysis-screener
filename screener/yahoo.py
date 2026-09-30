"""Key-free Yahoo research candles; no executable quote is claimed."""
from datetime import datetime

from .shared import DataError
from .engine import validate
from .feeds import NY, aggregate, stock_bars


def calendar(now):
    try:
        import exchange_calendars as xcals
    except ImportError:
        raise DataError('Install stock dependencies: pip install -r requirements-screener.txt') from None
    start = datetime.fromtimestamp(now-800*86400, NY).date().isoformat()
    end = datetime.fromtimestamp(now+14*86400, NY).date().isoformat()
    schedule = xcals.get_calendar('XNYS', start=start, end=end).schedule
    return [(row.open.timestamp(), row.close.timestamp()) for row in schedule.itertuples()]


def stock(symbol, sessions, cache, now):
    try:
        import yfinance as yf
    except ImportError:
        raise DataError('Install stock dependencies: pip install -r requirements-screener.txt') from None
    expected = max((t+1800 for op, cl in sessions for t in range(int(op), int(cl), 1800)
                    if t+1800 <= now), default=0)
    market_open = any(op <= now < cl for op, cl in sessions)
    key = 'yfinance:'+symbol
    saved = cache.get(key, {})
    if saved.get('end') == expected:
        return dict(saved['bundle'], market_open=market_open)

    try:
        ticker = yf.Ticker(symbol.replace('.', '-'))
        daily_frame = ticker.history(period='2y', interval='1d', auto_adjust=False,
                                     actions=False, prepost=False, repair=False, keepna=True, timeout=20)
        # Fetch native 15m bars: yfinance resamples 30m and can hide a missing half.
        intraday_frame = ticker.history(period='60d', interval='15m', auto_adjust=False,
                                        actions=False, prepost=False, repair=False, keepna=True, timeout=20)
        metadata = ticker.history_metadata
    except Exception:
        # Library exceptions can contain remote response bodies; keep errors curated.
        raise DataError('Yahoo download failed; check connection or rate limits') from None
    if daily_frame.empty or intraday_frame.empty:
        raise DataError('Missing Yahoo price history; check symbol or rate limits')
    if metadata.get('currency') != 'USD' or metadata.get('exchangeTimezoneName') != 'America/New_York':
        raise DataError('Yahoo stock provider supports US listings in USD only')
    by_date = {datetime.fromtimestamp(op, NY).date(): (op, cl) for op, cl in sessions}
    daily, intraday = [], []
    for frame, duration, bars in ((daily_frame, None, daily), (intraday_frame, 900, intraday)):
        if frame.index.tz is None:
            raise DataError('Yahoo candles have no exchange timezone')
        for when, row in frame.iterrows():
            session = by_date.get(when.tz_convert(NY).date())
            if session is None:
                raise DataError('Yahoo candle outside the US session calendar')
            op, cl = session
            start, end = (when.timestamp(), when.timestamp()+duration) if duration else (op, cl)
            if end > now:
                continue
            if not op <= start < end <= cl or (duration and (start-op) % duration):
                raise DataError('Yahoo candle outside regular session boundaries')
            bars.append(dict(start=start, end=end,
                             **{name:float(row[name.title()]) for name in ('open','high','low','close','volume')}))
        validate(bars, now)
    if not daily:
        raise DataError('Missing completed Yahoo daily candles')
    expected_days = [(op, cl) for op, cl in sessions if op >= daily[0]['start'] and cl <= now]
    if [(b['start'], b['end']) for b in daily] != expected_days:
        raise DataError('Missing Yahoo daily session')
    if not intraday:
        raise DataError('Missing completed Yahoo intraday candles')
    indexed = {b['start']: b for b in intraday}
    regular = []
    for op, cl in sessions:
        if cl <= intraday[0]['start'] or op > now:
            continue
        for start in range(int(op), int(cl), 1800):
            if start+1800 > now:
                break
            if start not in indexed or start+900 not in indexed:
                raise DataError('Missing regular-session 15-minute candle')
            regular.append(aggregate([indexed[start], indexed[start+900]]))
    _, hourly = stock_bars(regular, sessions, now)
    if not hourly or regular[-1]['end'] != expected:
        raise DataError('Missing latest completed Yahoo intraday candle')
    bundle = dict(symbol=symbol, market='stocks', source='Yahoo Finance / yfinance / split adjusted / regular session / research feed',
                  setup=daily[-250:], hourly=hourly[-250:], execution=regular[-500:], sessions=sessions,
                  quote=None, quote_error='Yahoo research feed has no verified executable quote; entries blocked',
                  market_open=market_open)
    cache[key] = dict(end=expected, bundle=bundle)
    return bundle
