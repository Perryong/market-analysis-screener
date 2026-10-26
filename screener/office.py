"""Agent office: a local page that shows the pipeline as agents at desks, live, plus a run calendar.

python -m screener office  →  http://127.0.0.1:8765  (localhost only; read-only market data)
Jobs run as child `python -m screener …` processes with SCREENER_PROGRESS=1; their JSON progress
lines (shared.progress) are relayed to the page over Server-Sent Events.
"""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / 'history'
# The real `report` (Telegram send + git push) is deliberately not runnable from a click.
JOBS = {'demo': ['demo'], 'scan': ['once'], 'report': ['report', '--dry-run']}
PAGE = Path(__file__).with_name('office.html')  # read per request, so edits show on reload
STAMP = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{6}$')
COMBINED = re.compile(r'COMBINED \([^)]*\): (\w+)')


def _parse(text):
    signal = combined = None
    read, section = [], None
    for line in text.splitlines():
        if line.startswith('**Signal:**'):
            signal = line[len('**Signal:**'):].strip()
        elif line.startswith('**COMBINED'):
            m = COMBINED.search(line)
            combined = m.group(1) if m else None
        elif line.startswith('## '):
            section = line[3:].strip()
        elif section in ('Verified read', 'AI read'):
            if line.startswith(('*Analysis only', '[Evidence')):
                section = None
            elif line.strip():
                read.append(line.strip())
    return dict(signal=signal, combined=combined, read=' '.join(read))


def calendar(history=HISTORY):
    """Runs (one per stamp) newest first, each with per-symbol combined reads and counts."""
    runs = {}
    for md in history.glob('*/*.md'):
        if not STAMP.match(md.stem):
            continue
        info = _parse(md.read_text(encoding='utf-8', errors='replace'))
        run = runs.setdefault(md.stem, dict(stamp=md.stem, date=md.stem[:10],
                                            time=md.stem[11:13] + ':' + md.stem[13:15], symbols=[], counts={}))
        run['symbols'].append(dict(symbol=md.parent.name, combined=info['combined'], signal=info['signal']))
        key = info['combined'] or 'OTHER'
        run['counts'][key] = run['counts'].get(key, 0) + 1
    for run in runs.values():
        run['symbols'].sort(key=lambda s: s['symbol'])
    return sorted(runs.values(), key=lambda r: r['stamp'], reverse=True)


def run_detail(stamp, history=HISTORY):
    if not STAMP.match(stamp):  # stamp comes from the URL; it builds a file path
        raise ValueError('bad stamp')
    out = []
    for md in sorted(history.glob(f'*/{stamp}.md')):
        info = _parse(md.read_text(encoding='utf-8', errors='replace'))
        out.append(dict(info, symbol=md.parent.name, path=str(md.relative_to(history.parent))))
    return dict(stamp=stamp, symbols=out)


class Office:
    """One job at a time; an append-only event log that SSE clients follow by index."""

    def __init__(self, config):
        self.config = config
        self.cond = threading.Condition()
        self.log = []
        self.proc = None
        self.job = None
        self.job_start = 0

    def _emit(self, event):
        with self.cond:
            event['seq'] = len(self.log)
            self.log.append(event)
            self.cond.notify_all()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, job):
        if job not in JOBS:
            raise KeyError(job)
        with self.cond:
            if self.running():
                return False
            env = dict(os.environ, SCREENER_PROGRESS='1', PYTHONUNBUFFERED='1')
            cmd = [sys.executable, '-m', 'screener', *JOBS[job], '--config', str(self.config)]
            self.job, self.job_start = job, len(self.log)
            self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, text=True)
            self._emit(dict(type='start', job=job))
        threading.Thread(target=self._follow, args=(self.proc,), daemon=True).start()
        return True

    def _follow(self, proc):
        last = ''
        for line in proc.stderr:
            line = line.rstrip('\n')
            try:
                step = json.loads(line)
                if not isinstance(step, dict) or 'agent' not in step:
                    raise ValueError
                self._emit(dict(step, type='step'))
            except ValueError:
                if line.strip():
                    last = line
                    self._emit(dict(type='line', text=line[:500]))
        code = proc.wait()
        self._emit(dict(type='end', job=self.job, code=code, error=last[:500] if code else None))

    def status(self):
        with self.cond:
            return dict(running=self.running(), job=self.job, seq=len(self.log),
                        from_seq=self.job_start if self.running() else len(self.log))


def handler_for(office):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, kind='application/json'):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path, _, query = self.path.partition('?')
            if path == '/':
                return self._send(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
            if path == '/status':
                return self._send(200, json.dumps(office.status()))
            if path == '/calendar.json':
                return self._send(200, json.dumps(calendar()))
            if path.startswith('/calendar/') and path.endswith('.json'):
                try:
                    return self._send(200, json.dumps(run_detail(path[len('/calendar/'):-5])))
                except ValueError:
                    return self._send(400, '{"error":"bad stamp"}')
            if path == '/events':
                return self._events(query)
            self._send(404, '{"error":"not found"}')

        def do_POST(self):
            if self.path != '/run':
                return self._send(404, '{"error":"not found"}')
            # JSON content type forces a CORS preflight, so other sites cannot trigger runs.
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self._send(415, '{"error":"json only"}')
            try:
                length = min(int(self.headers.get('Content-Length', 0)), 1000)
                job = json.loads(self.rfile.read(length) or b'{}').get('job')
                started = office.start(job)
            except (KeyError, ValueError, AttributeError):
                return self._send(400, '{"error":"unknown job"}')
            if not started:
                return self._send(409, '{"error":"a job is already running"}')
            self._send(202, json.dumps(dict(started=job)))

        def _events(self, query):
            m = re.search(r'(?:^|&)from=(\d+)', query)
            i = int(m.group(1)) if m else office.status()['from_seq']
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                while True:
                    with office.cond:
                        office.cond.wait_for(lambda: len(office.log) > i, timeout=15)
                        batch = office.log[i:]
                    i += len(batch)
                    chunk = ''.join(f'data: {json.dumps(e)}\n\n' for e in batch) or ': ping\n\n'
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(config, port=8765):
    office = Office(Path(config).resolve())
    server = ThreadingHTTPServer(('127.0.0.1', port), handler_for(office))
    server.daemon_threads = True
    print(f'Agent office · http://127.0.0.1:{port}  (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
