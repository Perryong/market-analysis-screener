"""Persistent signals, simulated fills, configuration, and scheduled scans."""
from contextlib import contextmanager
from pathlib import Path
import json
import math
import os
import re
import sqlite3
import time

from .shared import json_text
from .shared import DataError, fingerprint
from .shared import send
from . import engine, feeds


def default_config():
    return dict(
        stocks=dict(enabled=True, provider='yfinance', environment='paper', feed='sip',
                    symbols='AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX CRM ORCL ADBE INTC MU QCOM AMAT JPM BAC GS V MA WMT COST HD UNH JNJ LLY ABBV XOM CVX CAT GE BA UBER PLTR COIN HOOD SHOP SPY QQQ'.split()),
        crypto=dict(enabled=True, symbols='BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT ADAUSDT LINKUSDT AVAXUSDT SUIUSDT LTCUSDT BCHUSDT DOTUSDT UNIUSDT NEARUSDT AAVEUSDT TRXUSDT ATOMUSDT FILUSDT ARBUSDT OPUSDT INJUSDT HBARUSDT XLMUSDT ETCUSDT ICPUSDT RENDERUSDT TAOUSDT PEPEUSDT'.split()),
        strategy=dict(engine.DEFAULTS),
        paper=dict(enabled=True, capital=10000, risk_fraction=.0025,
                   max_notional_fraction=.1, max_total_fraction=.3))


def validate_config(config):
    if set(config) != {'stocks','crypto','strategy','paper'}:
        raise DataError('Config requires stocks, crypto, strategy and paper sections')
    for market in ('stocks','crypto'):
        c = config[market]
        if type(c['enabled']) is not bool or not isinstance(c['symbols'], list) or not c['symbols']:
            raise DataError('Each market needs enabled and a nonempty symbol list')
        if len(c['symbols']) > 500 or len(set(c['symbols'])) != len(c['symbols']):
            raise DataError('Duplicate symbols or universe exceeds 500 per market')
        if any(not isinstance(s,str) or not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,19}',s) for s in c['symbols']):
            raise DataError('Invalid symbol')
    if config['stocks'].get('provider', 'alpaca') not in ('alpaca', 'yfinance'):
        raise DataError('Stocks provider must be alpaca or yfinance')
    if config['stocks'].get('provider', 'alpaca') == 'alpaca' and (config['stocks']['feed'] not in ('iex','sip') or config['stocks']['environment'] not in ('paper','live')):
        raise DataError('Stocks require feed iex/sip and environment paper/live')
    if set(config['strategy']) != set(engine.DEFAULTS):
        raise DataError('Unknown or missing strategy settings')
    for k,v in config['strategy'].items():
        if k == 'round_trip_cost_bps' and v is None:
            continue
        if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or v < 0:
            raise DataError('Strategy settings must be finite nonnegative numbers')
        if k in ('lookback','retest_hours') and (type(v) is not int or not 1 <= v <= 100):
            raise DataError('Invalid candle count')
    if config['strategy']['lookback'] < 20 or config['strategy']['min_rr'] <= 0:
        raise DataError('lookback must be >=20 and min_rr >0')
    p = config['paper']
    if type(p['enabled']) is not bool:
        raise DataError('paper.enabled must be boolean')
    for k in ('capital','risk_fraction','max_notional_fraction','max_total_fraction'):
        if isinstance(p[k],bool) or not isinstance(p[k],(int,float)) or not math.isfinite(p[k]) or p[k] <= 0:
            raise DataError('Invalid paper budget')
    if not p['risk_fraction'] <= p['max_notional_fraction'] <= p['max_total_fraction'] <= 1:
        raise DataError('Paper fractions must satisfy risk <= position <= total <= 1')
    return config


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at REAL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (event TEXT, recipient TEXT, status TEXT,
                PRIMARY KEY(event,recipient));''')

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(key,json_text(value)))

    def trades(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT value FROM trades ORDER BY rowid')]

    def trade(self, value):
        self.db.execute('INSERT OR REPLACE INTO trades VALUES (?,?)',(value['id'],json_text(value)))

    def close(self):
        self.db.close()


class Cache:
    def __init__(self, store):
        self.store = store

    def get(self, key, default=None):
        return self.store.get('cache:'+key, default)

    def __setitem__(self, key, value):
        self.store.put('cache:'+key,value)


def process(store, bundle, settings, paper, now):
    key = bundle['market']+':'+bundle['symbol']
    saved = store.get('signal:'+key)
    context = fingerprint([bundle.get('source'), {k:v for k,v in settings.items() if k not in ('round_trip_cost_bps','slippage_bps')}])
    try:
        if saved and saved.get('context',context) != context:
            raise DataError('Feed or strategy changed; use a new state directory')
        result, state, events = engine.evaluate(bundle,saved,settings,now)
    except DataError:
        # Keep unknown exposure visible even when signal history cannot resume.
        for p in store.trades():
            if p['key']==key and p['status']=='OPEN':
                store.trade(dict(p,status='UNSCORABLE',exit_reason='SIGNAL_HISTORY_UNVERIFIED'))
        raise
    state['context'] = context
    result.update(source=bundle.get('source','recorded data'), quote=bundle.get('quote'),
                  quote_error=bundle.get('quote_error'), candles=bundle['setup'][-40:])
    store.db.execute('BEGIN IMMEDIATE')
    with store.db:
        trades = store.trades()
        for p in trades:
            if p['key'] == key and p['status'] == 'OPEN':
                updated = engine.paper_step(p,bundle.get('execution',bundle['hourly']),bundle.get('sessions',[]))
                store.trade(updated)
        trades = store.trades()
        active = [p for p in trades if p['status'] in ('OPEN','UNSCORABLE')]
        if result['status'] == 'ENTRY_ELIGIBLE':
            result['paper_note'] = 'Breakdown is informational; no borrow or derivatives model' if result['side']=='SHORT' else 'Paper disabled'
        if paper['enabled'] and result['status']=='ENTRY_ELIGIBLE' and result['side']=='LONG':
            ident = fingerprint([key,state['trigger']])
            if not any(p['id']==ident or (p['key']==key and p['status'] in ('OPEN','UNSCORABLE')) for p in trades):
                exposure = sum(p['entry']*p['quantity'] for p in active if p['market']==bundle['market'])
                budget = paper['capital']*paper['risk_fraction']
                unit_risk = result['entry']-result['stop']+result['entry']*(settings['round_trip_cost_bps']+settings['slippage_bps'])/10000
                notional = min(paper['capital']*paper['max_notional_fraction'], paper['capital']*paper['max_total_fraction']-exposure)
                quantity = max(0,min(budget/unit_risk,notional/result['entry']))
                if bundle['market']=='stocks':
                    quantity = math.floor(quantity)
                if quantity > 0:
                    trade = dict(id=ident,key=key, symbol=bundle['symbol'],market=bundle['market'],
                                 currency='USD' if bundle['market']=='stocks' else 'USDT',
                                 status='OPEN',entry=result['entry'],entry_at=now,
                                 stop=result['stop'],target=result['target'],quantity=quantity,
                                 last_end=bundle['hourly'][-1]['end'],cost_bps=settings['round_trip_cost_bps'],
                                 slippage_bps=settings['slippage_bps'],regime=bundle['regime'])
                    store.trade(trade)
                    result['paper_note'] = 'Simulated entry at a fresh post-confirmation quote'
                else:
                    result['paper_note'] = 'Paper exposure cap / minimum share size'
        for ev in events:
            ev.update(symbol=bundle['symbol'],market=bundle['market'])
            store.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',
                             (fingerprint(ev),ev['at'],json_text(ev)))
        store.put('signal:'+key,state)
        store.put('result:'+key,result)
    return result


def scan(store, config, now, scheduled=False, only=None):
    data = feeds.MarketData(config,Cache(store),now)
    errors, updates = [], 0
    for market in ('stocks','crypto'):
        if not config[market]['enabled'] or (only and market != only):
            continue
        try:
            sessions = data.calendar() if market=='stocks' else []
            active = any((store.get('signal:'+market+':'+s) or {}).get('status') in engine.ACTIVE for s in config[market]['symbols'])
            active = active or any(t['market']==market and t['status']=='OPEN' for t in store.trades())
            due = feeds.slot(market,now,sessions,active)
            full_due = feeds.slot(market,now,sessions,False)
            last = store.get('job:'+market,{})
            if scheduled and (due is None or last.get('done',0)>=due or now-last.get('attempt',0)<300):
                continue
            store.put('job:'+market, dict(last,attempt=now))
            full_scan = not scheduled or last.get('full_done',0) < (full_due or 0)
            try:
                benchmark = data.stock('SPY')['setup'] if market=='stocks' else data.crypto('BTCUSDT',daily=True)
                context = engine.regime(benchmark)
            except (DataError,KeyError,TypeError,ValueError,IndexError):
                context = 'UNKNOWN'
                benchmark = []
            failed = False
            symbols = list(dict.fromkeys(config[market]['symbols'] +
                [t['symbol'] for t in store.trades() if t['market']==market and t['status']=='OPEN']))
            # Symbols removed from the config drop off the dashboard; their signal history is kept.
            prefix = 'result:'+market+':'
            for (key,) in store.db.execute('SELECT key FROM kv WHERE substr(key,1,?)=?',(len(prefix),prefix)).fetchall():
                if key[len(prefix):] not in symbols:
                    store.db.execute('DELETE FROM kv WHERE key=?',(key,))
            for symbol in symbols:
                key = market+':'+symbol
                if not full_scan and not ((store.get('signal:'+key) or {}).get('status') in engine.ACTIVE or
                    any(t['key']==key and t['status']=='OPEN' for t in store.trades())):
                    continue
                try:
                    bundle = data.stock(symbol) if market=='stocks' else data.crypto(symbol)
                    bundle['regime'] = context
                    bundle['benchmark'] = benchmark
                    checked = max(now,time.time())
                    paper_settings = config['paper'] if symbol in config[market]['symbols'] else dict(config['paper'],enabled=False)
                    process(store,bundle,config['strategy'],paper_settings,checked)
                    store.put('bundle:'+key, bundle)
                    updates += 1
                except (DataError,KeyError,TypeError,ValueError,IndexError) as exc:
                    failed = True
                    # DataError messages are curated; payload/parser exceptions stay private.
                    reason = str(exc) if isinstance(exc,DataError) else 'Provider schema invalid'
                    store.put('result:'+key,dict(symbol=symbol,market=market,status='DATA_UNAVAILABLE',
                              regime=context,reasons=[reason],checked_at=now,score=0))
                    errors.append(key+': '+reason)
            store.put('job:'+market,dict(attempt=now,done=last.get('done',0) if failed else due or now,
                      full_done=full_due if full_scan and not failed else last.get('full_done',0)))
        except (DataError,KeyError,TypeError,ValueError,IndexError) as exc:
            reason = str(exc) if isinstance(exc,DataError) else 'Provider schema invalid'
            errors.append(market+': '+reason)
            for symbol in config[market]['symbols']:
                store.put('result:'+market+':'+symbol,dict(symbol=symbol,market=market,status='DATA_UNAVAILABLE',
                          regime='UNKNOWN',reasons=[reason],checked_at=now,score=0))
    return updates, errors


def notify(store, now):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    recipients = list(dict.fromkeys(s.strip() for s in
        (os.getenv('TELEGRAM_CHAT_ID','')+','+os.getenv('TELEGRAM_ADDITIONAL_CHAT_IDS','')).split(',') if s.strip()))
    if not token or not recipients:
        raise DataError('Telegram credentials and recipient required for --notify')
    events = store.db.execute('SELECT id,value FROM events WHERE at>=? ORDER BY at',(now-5400,)).fetchall()
    for ident, raw in events:
        e = json.loads(raw)
        if e['status']=='WATCHING':
            continue
        result = store.get('result:'+e['market']+':'+e['symbol'], {})
        if result.get('status') != e['status']:
            continue
        text = (f"SCREENER · {e['symbol']} · {e['side'] or '—'}\n{e['status']} · {e['regime']}\n"
                f"Level {result.get('level','—')} · Stop {result.get('stop','—')} · Target {result.get('target','—')}\n"
                'Research alert; no broker order. '+', '.join(result.get('reasons',[])))
        for recipient in recipients:
            digest = fingerprint(recipient)
            if store.db.execute('SELECT 1 FROM receipts WHERE event=? AND recipient=?',(ident,digest)).fetchone():
                continue
            store.db.execute('INSERT INTO receipts VALUES (?,?,?)',(ident,digest,'UNCERTAIN'))
            try:
                send(token,recipient,text,None)
            except DataError:
                raise DataError('Telegram send unconfirmed; receipt retained to prevent blind retry') from None
            store.db.execute('UPDATE receipts SET status=? WHERE event=? AND recipient=?',('SENT',ident,digest))


@contextmanager
def locked(path):
    # ponytail: one local writer; use a database lock service if deployed on multiple hosts.
    import fcntl
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with open(path,'a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise DataError('Another screener run is active') from None
        yield
