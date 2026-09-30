"""Run with python -m screener --help. No command submits broker orders."""
import argparse
import json
from pathlib import Path
import sys
import time

from .shared import atomic_text, json_text
from .shared import DataError, fingerprint
from .runtime import Store, validate_config, locked, scan, notify
from .replay import demo, replay
from .view import render


def publish(store, out, mode, now):
    results = [json.loads(r[0]) for r in store.db.execute("SELECT value FROM kv WHERE key LIKE 'result:%'")]
    priority = {s:i for i,s in enumerate(('ENTRY_ELIGIBLE','RETESTED','CONFIRMED','DEVELOPING','WATCHING','INVALIDATED','MISSED','EXPIRED','DATA_UNAVAILABLE'))}
    results.sort(key=lambda r:(priority.get(r['status'],99),-r.get('score',0),r['symbol']))
    events = [json.loads(r[0]) for r in store.db.execute('SELECT value FROM events ORDER BY at')]
    payload = dict(version=1,generated_at=now,mode=mode,results=results,trades=store.trades(),events=events)
    atomic_text(out/'latest.json',json_text(payload))
    atomic_text(out/'index.html',render(payload))
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('once','tick','demo','replay','export'))
    parser.add_argument('--config',type=Path,default=Path('screener.json'))
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--out',type=Path)
    parser.add_argument('--market',choices=('stocks','crypto'))
    parser.add_argument('--symbols',help='Comma-separated override for --market')
    parser.add_argument('--bundle',type=Path,help='Replay input or export output file')
    parser.add_argument('--notify',action='store_true',help='Send fresh transitions to configured Telegram recipients')
    args = parser.parse_args(argv)
    mode = args.command if args.command in ('demo','replay') else 'live'
    state_dir = args.state_dir or Path('.screener')/mode
    out = args.out or state_dir/'public'
    if args.notify and args.command not in ('once','tick'):
        parser.error('--notify is only available for live once/tick commands')
    if args.symbols and not args.market:
        parser.error('--symbols requires --market')
    if args.command in ('export','replay') and not args.bundle:
        parser.error('--bundle is required')
    now = time.time()
    try:
        config = json.loads(args.config.read_text())
        if args.symbols:
            config[args.market]['symbols'] = [s.strip().upper() for s in args.symbols.split(',')]
        validate_config(config)
        with locked(state_dir/'run.lock'):
            store = Store(state_dir/'journal.sqlite3')
            try:
                old_mode = store.get('mode')
                if old_mode and old_mode != mode:
                    raise DataError('State directory belongs to another mode; choose a separate directory')
                store.put('mode',mode)
                errors = []
                if args.command == 'export':
                    if not args.market or not args.symbols or ',' in args.symbols:
                        raise DataError('Export requires --market and one --symbols value')
                    bundle = store.get('bundle:'+args.market+':'+config[args.market]['symbols'][0])
                    if not bundle:
                        raise DataError('No saved bundle; run a live scan first')
                    atomic_text(args.bundle,json_text(bundle))
                    print('Exported normalized research bars to',args.bundle)
                    return 0
                if args.command == 'demo':
                    # Repeat demo invocations get a fresh synthetic timeline, isolated from live state.
                    with store.db:
                        store.db.execute("DELETE FROM kv WHERE key!='mode'")
                        store.db.execute('DELETE FROM trades')
                        store.db.execute('DELETE FROM events')
                    demo(store,now,config['strategy'],config['paper'])
                elif args.command == 'replay':
                    bundle = json.loads(args.bundle.read_text())
                    ident = fingerprint([bundle,config['strategy'],config['paper']])
                    previous = store.get('replay_id')
                    if previous and previous != ident:
                        raise DataError('Replay state contains another experiment; choose a new --state-dir')
                    if not previous:
                        replay(store,bundle,config['strategy'],config['paper'])
                        store.put('replay_id',ident)
                else:
                    _, errors = scan(store,config,now,scheduled=args.command=='tick',only=args.market)
                    if args.notify:
                        try:
                            notify(store,now)
                        except DataError as exc:
                            errors.append(str(exc))
                payload = publish(store,out,mode,now)
                counts = {}
                for result in payload['results']:
                    counts[result['status']] = counts.get(result['status'],0)+1
                print(json.dumps(dict(mode=mode,states=counts,paper_records=len(payload['trades']),dashboard=str((out/'index.html').resolve()))))
                for error in errors:
                    print(error,file=sys.stderr)
                return 1 if errors else 0
            finally:
                store.close()
    except (DataError,OSError,ValueError,KeyError,TypeError) as exc:
        print('Screener failed: '+(str(exc) if isinstance(exc,DataError) else 'Invalid configuration, input or storage; check paths and schema'),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
