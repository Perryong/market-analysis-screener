import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import office
from screener.shared import progress

MD = """# {sym} · 2026-10-27 00:45:56 +08

**Signal:** {sym} · INVALIDATED · 15m · 1.29

**4H direction:** Bearish

**COMBINED (4H+1H): {comb} — reason**

## Previous read

old text

## {heading}

Fresh read line one.
Line two.

*Analysis only · No orders placed*
"""


class OfficeTest(unittest.TestCase):
    def history(self):
        tmp = Path(tempfile.mkdtemp()) / 'history'
        for sym, comb, heading in [('XRPUSDT', 'SELL', 'AI read'), ('AAPL', 'BUY', 'Verified read')]:
            (tmp / sym).mkdir(parents=True)
            (tmp / sym / '2026-10-27_004556.md').write_text(MD.format(sym=sym, comb=comb, heading=heading))
        (tmp / 'AAPL' / '2026-10-26_204556.md').write_text(MD.format(sym='AAPL', comb='HOLD', heading='AI read'))
        (tmp / 'AAPL' / 'notes.md').write_text('ignored')
        return tmp

    def test_calendar_groups_runs_by_stamp(self):
        runs = office.calendar(self.history())
        self.assertEqual([r['stamp'] for r in runs], ['2026-10-27_004556', '2026-10-26_204556'])
        first = runs[0]
        self.assertEqual((first['date'], first['time']), ('2026-10-27', '00:45'))
        self.assertEqual([s['symbol'] for s in first['symbols']], ['AAPL', 'XRPUSDT'])
        self.assertEqual(first['counts'], {'BUY': 1, 'SELL': 1})

    def test_run_detail_reads_only_current_read(self):
        detail = office.run_detail('2026-10-27_004556', self.history())
        aapl = detail['symbols'][0]
        self.assertEqual(aapl['read'], 'Fresh read line one. Line two.')
        self.assertTrue(aapl['signal'].startswith('AAPL · INVALIDATED'))
        with self.assertRaises(ValueError):
            office.run_detail('../../etc/passwd', self.history())

    def test_export_builds_static_read_only_site(self):
        hist = self.history()
        (hist / 'XRPUSDT' / '2026-10-27_004556.md').write_text(
            MD.format(sym='XRPUSDT', comb='SELL', heading='AI read') + 'https://www.tradingview.com/chart/?symbol=BINANCE:XRPUSDT&interval=240\n')
        out = hist.parent / 'site'
        self.assertEqual(office.export(out, hist), 2)
        page = (out / 'index.html').read_text()
        self.assertIn('window.OFFICE_STATIC=1', page)
        runs = json.loads((out / 'calendar.json').read_text())
        xrp = next(s for s in runs[0]['symbols'] if s['symbol'] == 'XRPUSDT')
        self.assertEqual((xrp['market'], xrp['status'], xrp['combined']), ('crypto', 'INVALIDATED', 'SELL'))
        detail = json.loads((out / 'runs' / '2026-10-27_004556.json').read_text())
        self.assertEqual(len(detail['symbols']), 2)

    def test_publish_site_copies_dashboard_and_office(self):
        from screener import history
        root = Path(tempfile.mkdtemp())
        public, site = root / 'public', root / 'docs'
        public.mkdir(); site.mkdir()
        (public / 'index.html').write_text('<a href="office/">')
        (public / 'latest.json').write_text('{}')
        paths = history.publish_site(public, site, self.history())
        self.assertEqual(paths, ['docs/index.html', 'docs/latest.json', 'docs/office/'])
        self.assertEqual((site / 'latest.json').read_text(), '{}')
        self.assertTrue((site / 'office' / 'calendar.json').exists())

    def test_progress_silent_unless_enabled(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {'SCREENER_PROGRESS': ''}), redirect_stderr(buf):
            progress('feed', 'fetch', 'AAPL')
        self.assertEqual(buf.getvalue(), '')
        with mock.patch.dict(os.environ, {'SCREENER_PROGRESS': '1'}), redirect_stderr(buf):
            progress('engine', 'evaluate', 'AAPL', status='CONFIRMED')
        line = json.loads(buf.getvalue())
        self.assertEqual((line['agent'], line['symbol'], line['status']), ('engine', 'AAPL', 'CONFIRMED'))

    def test_one_job_at_a_time_and_unknown_jobs_rejected(self):
        o = office.Office(Path('screener.json'))
        with self.assertRaises(KeyError):
            o.start('demo')  # only the live scan is runnable from the page
        fake = mock.Mock()
        fake.poll.return_value = None
        fake.stderr = iter(['{"agent":"feed","action":"fetch","symbol":"X"}\n', 'boom\n'])
        fake.wait.return_value = 1
        with mock.patch.object(office.subprocess, 'Popen', return_value=fake) as popen:
            self.assertTrue(o.start('scan'))
            self.assertFalse(o.start('scan'))  # first job still "running"
            cmd = popen.call_args[0][0]
            self.assertEqual(cmd[1:4], ['-m', 'screener', 'once'])
            self.assertEqual(popen.call_args[1]['env']['SCREENER_PROGRESS'], '1')
        for _ in range(50):
            if o.log and o.log[-1]['type'] == 'end':
                break
            time.sleep(0.02)
        self.assertEqual([e['type'] for e in o.log], ['start', 'step', 'line', 'end'])
        self.assertEqual((o.log[-1]['code'], o.log[-1]['error']), (1, 'boom'))


if __name__ == '__main__':
    unittest.main()
