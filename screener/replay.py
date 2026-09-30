"""Deterministic recorded-bar replay and offline synthetic demonstration."""
from . import engine
from .runtime import process
from .shared import DataError


def verify_coverage(bundle):
    market = bundle['market']
    if market not in ('stocks','crypto'):
        raise DataError('Unknown replay market')
    for name in ('setup','hourly'):
        bars = bundle[name]
        if not bars:
            raise DataError('Missing replay candles')
        if market=='crypto':
            duration = 14400 if name=='setup' else 3600
            if any(b['end']-b['start']!=duration for b in bars) or any(a['end']!=b['start'] for a,b in zip(bars,bars[1:])):
                raise DataError('Missing or malformed crypto replay candles')
        else:
            sessions=bundle.get('sessions',[])
            expected=[]
            for op,cl in sessions:
                intervals = [(op,cl)] if name=='setup' else [(t,t+3600) for t in range(int(op),int(cl)-3599,3600)]
                expected += [(a,b) for a,b in intervals if a>=bars[0]['start'] and b<=bars[-1]['end']]
            if not expected or expected != [(b['start'],b['end']) for b in bars]:
                raise DataError('Missing stock replay candles or session calendar')


def replay(store, bundle, settings, paper):
    """Observe closed bars, then simulate the next hourly open. No historical quotes claimed."""
    for name in ('setup','hourly','benchmark'):
        engine.validate(bundle.get(name,[]),float('inf'))
    verify_coverage(bundle)
    count = 0
    for i, bar in enumerate(bundle['hourly']):
        when = bar['start']
        setup = [b for b in bundle['setup'] if b['end'] <= when]
        hours = bundle['hourly'][:i]
        if len(setup) < max(22,settings['lookback']+1) or len(hours) < 15:
            continue
        context = engine.regime([b for b in bundle.get('benchmark',[]) if b['end'] <= when])
        current = dict(bundle,setup=setup,hourly=hours,regime=context,market_open=True,
                       quote=dict(price=bar['open'],time=when),source='REPLAY / '+bundle.get('source','recorded bars'))
        if 'execution' in bundle:
            current['execution'] = [b for b in bundle['execution'] if b['end'] <= when]
        process(store,current,settings,paper,when)
        count += 1
    # Resolve the last execution bar without fabricating another entry opportunity.
    for position in store.trades():
        if position['key']==bundle['market']+':'+bundle['symbol'] and position['status']=='OPEN':
            store.trade(engine.paper_step(position,bundle.get('execution',bundle['hourly']),bundle.get('sessions',[])))
    return count


def demo(store, now, settings, paper):
    end = int(now)//3600*3600
    base = end-105*3600

    def b(n, duration=3600, o=100, h=110, l=90, c=100, v=100):
        return dict(start=base+n*duration,end=base+(n+1)*duration,open=o,high=h,low=l,close=c,volume=v)

    settings = dict(settings,round_trip_cost_bps=10)
    for symbol, outcome in [('BTCUSDT','entry'),('ETHUSDT','invalid'),('SOLUSDT','pending')]:
        bundle = dict(symbol=symbol,market='crypto',source='SYNTHETIC DEMO',regime='BULLISH',market_open=True,
                      setup=[b(i,14400) for i in range(25)],hourly=[b(i) for i in range(100)],
                      quote=dict(price=100,time=base+360000))
        process(store,bundle,settings,paper,base+360000)
        bundle['setup'].append(b(25,14400,109,114,108,113,200))
        bundle['hourly'] += [b(i,o=109,h=114,l=108,c=113) for i in range(100,104)]
        bundle['quote'] = dict(price=113,time=base+374403)
        process(store,bundle,settings,paper,base+374403)
        last = b(104,o=112,h=113,l=109.8,c=111) if outcome=='entry' else (
               b(104,o=112,h=113,l=108,c=109) if outcome=='invalid' else b(104,o=115,h=116,l=115,c=115.5))
        bundle['hourly'].append(last)
        bundle['quote'] = dict(price=last['close'],time=now)
        process(store,bundle,settings,paper,now)
        store.put('bundle:crypto:'+symbol,bundle)
