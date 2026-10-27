import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from screener import history, report, runtime, charts


class ReportingTest(unittest.TestCase):
    def setUp(self):
        self.store = runtime.Store(':memory:')
        self.addCleanup(self.store.close)
        self.now = 216003
        self.hours = [dict(start=i*3600, end=(i+1)*3600, open=100,
                           high=101, low=99, close=100, volume=100) for i in range(60)]
        self.setup = copy.deepcopy(self.hours)
        self.result = dict(status='WATCHING', side='LONG', upper=110, lower=90,
                           plan_entry=110, plan_stop=108, plan_target=130,
                           checked_at=self.now, setup_close=216000, hourly_close=216000,
                           regime='NEUTRAL', reasons=[])
        self.bundle = dict(quote=dict(price=100, time=self.now), market_open=True,
                           source='test fixture', setup=self.setup, hourly=self.hours)

    def card(self, result=None, **kwargs):
        return report.card(result or self.result, 'TEST', 'crypto', self.setup,
                           self.hours, self.bundle['quote']['price'], self.store,
                           dry_run=True, **kwargs)

    def test_watching_cannot_be_promoted_by_old_ai_prose(self):
        self.store.put('airead:crypto:TEST', 'The watching long has activated: confirmed long.')
        _, fields = self.card(cached_read=True)
        self.assertIn('No eligible entry', fields['ai_read'] or '')
        self.assertNotIn('has activated', fields['ai_read'])
        self.assertIsNone(fields['prev_read'])

    def test_developing_is_not_a_confirmed_breakout(self):
        _, fields = self.card(dict(self.result, status='DEVELOPING'))
        self.assertNotIn('broke above', fields['setup_txt'])
        self.assertNotIn('BUY entry', fields['top'])

    def test_planned_and_executable_entries_are_separate(self):
        result = dict(self.result, status='ENTRY_ELIGIBLE', entry=110.125,
                      stop=108.037, target=130, net_rr=9)
        self.bundle['quote']['price'] = 110.07
        _, fields = self.card(result)
        self.assertIn('snapshot', fields)
        self.assertEqual(fields['snapshot']['result']['entry'], 110.125)
        self.assertEqual(fields['snapshot']['result']['plan_entry'], 110)
        self.assertIn('110.125', fields['top'])

    def test_xrp_risk_levels_do_not_collapse_to_two_decimals(self):
        result = dict(self.result, status='RETESTED', side='SHORT',
                      plan_entry=1.3862, plan_stop=1.3897, plan_target=1.25)
        _, fields = self.card(result)
        self.assertIn('1.3862', fields['top'])
        self.assertIn('1.3897', fields['top'])
        self.assertNotIn('zero-tolerance', fields['ai_read'] or '')

    def test_snapshot_preserves_raw_evidence_and_does_not_mutate_strategy(self):
        original = copy.deepcopy(self.result)
        _, fields = self.card()
        self.assertIn('snapshot', fields)
        snap = fields['snapshot']
        self.assertEqual(snap['result'], original)
        self.assertEqual(snap['hourly'], self.hours)
        self.assertEqual(snap['setup'], self.setup)
        self.assertEqual(self.result, original)

    def test_model_cannot_inject_arbitrary_claims(self):
        self.assertTrue(hasattr(report, 'verified_read'), 'A validated rendering boundary is required')
        _, fields = self.card()
        bad = {'fact_ids': ['price'], 'commentary': 'BUY now; 522.07 is inside 515.5–521.47.'}
        text, mode = report.verified_read(fields['snapshot'], bad)
        self.assertNotIn('BUY now', text)
        self.assertIn('No eligible entry', text)
        self.assertEqual(mode, 'fallback: invalid AI selection')

    def test_price_comparisons_are_computed_not_generated(self):
        self.assertTrue(hasattr(report, 'verified_read'))
        _, fields = self.card()
        snap = fields['snapshot']
        for price, low, high, expected in [(522.07, 515.5, 521.47, 'above'),
                                            (740.33, 737.2, 737.9, 'above'),
                                            (520, 515.5, 521.47, 'inside')]:
            with self.subTest(price=price):
                snap['price']['value'] = price
                snap['fvgs_near'] = [('bull', low, high)]
                text, _ = report.verified_read(snap)
                self.assertIn(f'Price is {expected}', text)

    def test_removed_gap_is_not_automatically_a_fill(self):
        self.assertTrue(hasattr(report, 'verified_read'))
        _, fields = self.card()
        snap = fields['snapshot']
        snap['previous'] = dict(fvgs_near=[['bull', 90, 91]],
                                candle_ends=dict(hourly=212400), price=dict(value=100))
        text, _ = report.verified_read(snap)
        self.assertIn('no longer listed', text)
        self.assertIn('not evidence of a fill', text)

    def test_fvg_boundary_touch_records_bar_evidence(self):
        self.assertTrue(hasattr(report, 'verified_read'))
        _, fields = self.card()
        snap = fields['snapshot']
        snap['previous'] = dict(fvgs_near=[['bull', 99.5, 100.5]],
                                candle_ends=dict(hourly=212400), price=dict(value=100))
        text, _ = report.verified_read(snap)
        self.assertIn('boundary touched', text)
        self.assertNotIn('fully filled', text)

    def test_history_writes_a_full_precision_snapshot(self):
        _, fields = self.card()
        with tempfile.TemporaryDirectory() as tmp, patch.object(history, 'HISTORY', Path(tmp)):
            path = history.write('TEST', fields, b'fixture image', self.now)
            self.assertTrue(path.with_suffix('.json').exists())
            saved = json.loads(path.with_suffix('.json').read_text())
            self.assertEqual(saved['result']['plan_stop'], 108)
            self.assertEqual(saved['hourly'], self.hours)
            self.assertIn('Evidence', path.read_text())

    def test_repeated_inputs_are_not_new_confirmation(self):
        _, first = self.card()
        self.assertIn('snapshot', first)
        # Exercise the real persisted cache, with only the external model disabled.
        with patch.object(report, 'DEEPSEEK_KEY', None):
            args = (self.result, 'TEST', 'crypto', self.setup, self.hours, 100, self.store)
            report.card(*args, dry_run=False, now=self.now, bundle=self.bundle)
            _, second = report.card(*args, dry_run=False, now=self.now+30, bundle=self.bundle)
        self.assertIn('No new market evidence', second['ai_read'])

    def test_old_quote_blocks_reporting_live_entry_without_changing_status(self):
        self.assertTrue(hasattr(report, 'verified_read'))
        result = dict(self.result, status='ENTRY_ELIGIBLE', entry=110.125, stop=108.037, target=130)
        old = dict(self.bundle, quote=dict(price=110.07, time=self.now-3600))
        _, f = report.card(result, 'TEST', 'crypto', self.setup, self.hours, 110.07,
                           self.store, True, now=self.now, bundle=old)
        self.assertEqual(f['snapshot']['result']['status'], 'ENTRY_ELIGIBLE')
        self.assertIn('cannot verify a current eligible entry', f['ai_read'])
        self.assertIn('quote', f['data_note'].lower())

    def test_missing_market_data_does_not_inherit_previous_levels(self):
        self.store.put('airead:crypto:TEST', 'Buy at 243.37, stop 241.77.')
        _, f = self.card(dict(status='DATA_UNAVAILABLE'), cached_read=True)
        self.assertIn('No eligible entry', f['ai_read'] or '')
        self.assertNotIn('243.37', f['ai_read'])

    def test_valid_ai_selection_cannot_omit_strategy_or_evidence(self):
        _, f = self.card()
        text, mode = report.verified_read(f['snapshot'], {'fact_ids': ['price']})
        self.assertIn('Strategy: WATCHING', text)
        self.assertIn('No eligible entry', text)
        self.assertIn('timestamp', text)
        self.assertEqual(mode, 'AI-selected verified facts')

    def test_model_transport_failure_falls_back_without_losing_report(self):
        with patch.object(report, 'DEEPSEEK_KEY', 'test'), patch.object(
                report.urllib.request, 'urlopen', side_effect=OSError('offline')):
            _, f = report.card(self.result, 'TEST', 'crypto', self.setup, self.hours,
                               100, self.store, False, now=self.now, bundle=self.bundle)
        self.assertIn('No eligible entry', f['ai_read'])
        self.assertIn('fallback', f['read_mode'])

    def test_fresh_eligible_report_uses_actual_levels_and_never_claims_fill(self):
        self.bundle['quote']['price'] = 110.07
        r = dict(self.result, status='ENTRY_ELIGIBLE', entry=110.125, stop=108.037, target=130)
        _, f = report.card(r, 'TEST', 'crypto', self.setup, self.hours, 110.07,
                           self.store, True, now=self.now, bundle=self.bundle)
        self.assertIn('engine marks this entry eligible', f['ai_read'])
        self.assertIn('entry 110.125, stop 108.037', f['ai_read'])
        self.assertIn('no fill or position is implied', f['ai_read'])

    def test_history_preview_is_local_and_keeps_original_reports_unchanged(self):
        self.assertTrue(hasattr(report, 'preview_history'), 'Local history preview is required')
        source = Path(__file__).resolve().parents[1] / 'history' / 'XAUUSD' / '2026-10-09_164527.md'
        original = source.read_bytes()
        with tempfile.TemporaryDirectory() as tmp, patch.object(report, 'send', side_effect=AssertionError('No sends')), \
                patch.object(history, 'git_commit', side_effect=AssertionError('No commits')):
            root = Path(tmp) / 'history' / 'XAUUSD'
            root.mkdir(parents=True)
            (root / source.name).write_bytes(original)
            output = Path(tmp) / 'review'
            count = report.preview_history(root.parent, output)
            self.assertEqual(count, 1)
            page = (output / 'index.html').read_text()
            self.assertIn('Strategy: WATCHING. No eligible entry.', page)
            self.assertIn('Legacy archive: rounded values', page)
            self.assertIn('confirmed long', page)  # Old text remains visible only for comparison.
            audit = json.loads((output / 'evidence.json').read_text())
            self.assertFalse(audit[0]['snapshot']['full_precision'])
            self.assertEqual((root / source.name).read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)

    def test_preview_marks_timestamp_only_updates_as_repeated_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'history'
            for now in (self.now, self.now+30):
                r = dict(self.result, checked_at=now)
                bundle = dict(self.bundle, quote=dict(price=100, time=now))
                _, f = report.card(r, 'TEST', 'crypto', self.setup, self.hours, 100,
                                   self.store, True, now=now, bundle=bundle)
                with patch.object(history, 'HISTORY', root):
                    history.write('TEST', f, b'fixture image', now)
            report.preview_history(root, Path(tmp)/'review')
            saved = json.loads((Path(tmp)/'review/evidence.json').read_text())
            self.assertIn('No new market evidence', saved[1]['verified'])

    def test_chart_pivot_cross_is_not_an_entry_signal(self):
        sig = charts.signal_of(self.result)
        caption = charts.caption('TEST', 'WATCHING', sig, 111, 1,
                                 live=dict(side='LONG', price=111))
        self.assertNotIn('BUY ↑ now', caption)
        self.assertIn('WATCHING', caption)
        self.assertIn('not entry eligibility', caption)

    def test_chart_plans_preserve_risk_precision(self):
        sig = charts.signal_of(dict(self.result, status='RETESTED', side='SHORT',
                                    plan_entry=1.3862, plan_stop=1.3897))
        caption = charts.caption('XRPUSDT', 'RETESTED', sig, 1.387, 0)
        self.assertIn('conditional plan', caption)
        self.assertIn('1.3897', caption)
        crossed = charts.caption('XRPUSDT', 'WATCHING', sig, 1.3863, 0,
                                  live=dict(side='LONG', price=1.3863))
        self.assertIn('trigger 1.3862', crossed)

    def test_untrusted_model_response_is_rejected_through_real_card_path(self):
        responses = [dict(fact_ids=['price'], commentary='The watching long has activated'),
                     dict(fact_ids=['not_a_fact']), dict(fact_ids=['price', 'price']),
                     'BUY NOW', None]
        for response in responses:
            with self.subTest(response=response), patch.object(report, 'DEEPSEEK_KEY', 'test'):
                body = json.dumps({'choices': [{'message': {'content': json.dumps(response)}}]}).encode()
                with patch.object(report.urllib.request, 'urlopen', return_value=io.BytesIO(body)):
                    _, f = report.card(self.result, 'TEST', 'crypto', self.setup, self.hours,
                                       100, self.store, False, now=self.now, bundle=self.bundle)
                self.assertIn('No eligible entry', f['ai_read'])
                self.assertNotIn('has activated', f['ai_read'])
                self.assertEqual(f['read_mode'], 'fallback: invalid AI selection')
                self.store.db.execute('DELETE FROM kv')

    def test_valid_model_selection_is_rendered_without_previous_prose(self):
        self.store.put('airead:crypto:TEST', 'UNTRUSTED OLD BUY RECOMMENDATION')
        requests = []
        def answer(request, timeout):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': '{"fact_ids":["price"]}'}}]}).encode())
        with patch.object(report, 'DEEPSEEK_KEY', 'test'), patch.object(report.urllib.request, 'urlopen', side_effect=answer):
            _, f = report.card(self.result, 'TEST', 'crypto', self.setup, self.hours,
                               100, self.store, False, now=self.now, bundle=self.bundle)
        self.assertEqual(f['read_mode'], 'AI-selected verified facts')
        self.assertIn('No eligible entry', f['ai_read'])
        self.assertNotIn('UNTRUSTED OLD', json.dumps(requests))

    def test_cli_preview_does_not_open_live_store_or_make_external_calls(self):
        from screener.__main__ import main
        with tempfile.TemporaryDirectory() as tmp, patch('screener.__main__.Store', side_effect=AssertionError('No live state')), \
                patch.object(report.urllib.request, 'urlopen', side_effect=AssertionError('No network')):
            code = main(['report', '--preview-history', str(Path(__file__).resolve().parents[1]/'history'),
                         '--symbols', 'XAUUSD', '--out', tmp])
            self.assertEqual(code, 0)
            records = json.loads((Path(tmp)/'evidence.json').read_text())
            self.assertEqual({r['snapshot']['symbol'] for r in records}, {'XAUUSD'})


if __name__ == '__main__':
    unittest.main()
