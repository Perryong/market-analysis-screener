"""Offline coverage checks for the key-free US stock provider."""
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from screener import feeds, runtime


def at(value):
    return datetime.fromisoformat(value).timestamp()


class YahooTest(unittest.TestCase):
    def data(self):
        config = runtime.default_config()
        config['stocks']['provider'] = 'yfinance'
        return feeds.MarketData(config, {}, at('2026-11-30T11:05:00-05:00'))

    def frames(self, data):
        sessions = [(op, cl) for op, cl in data.sessions
                    if op >= at('2026-11-25T00:00:00-05:00') and op <= data.now]
        daily_index = [pd.Timestamp(op, unit='s', tz=feeds.NY).normalize() for op, cl in sessions]
        intraday_index = [pd.Timestamp(t, unit='s', tz=feeds.NY)
                          for op, cl in sessions for t in range(int(op), int(cl), 900)]
        def frame(index):
            return pd.DataFrame(dict(Open=100., High=102., Low=99., Close=101., Volume=100.), index=index)
        return frame(daily_index), frame(intraday_index)

    def test_calendar_without_keys_handles_holiday_early_close_and_dst(self):
        data = self.data()
        with patch.dict('os.environ', {}, clear=True), patch('screener.feeds.get_json', side_effect=AssertionError('Alpaca called')):
            sessions = data.calendar()
        by_date = {datetime.fromtimestamp(op, feeds.NY).date().isoformat(): (op, cl) for op, cl in sessions}
        self.assertNotIn('2026-11-26', by_date)
        op, cl = by_date['2026-11-27']
        self.assertEqual(cl-op, 3.5*3600)
        self.assertEqual(datetime.fromtimestamp(op, feeds.timezone.utc).hour, 14)
        self.assertEqual(datetime.fromtimestamp(by_date['2026-09-29'][0], feeds.timezone.utc).hour, 13)

    def test_completed_bars_and_no_fabricated_quote(self):
        data = self.data()
        data.calendar()
        daily, intraday = self.frames(data)
        with patch('yfinance.Ticker') as ticker:
            ticker.return_value.history.side_effect = [daily, intraday]
            ticker.return_value.history_metadata = {'currency':'USD', 'exchangeTimezoneName':'America/New_York'}
            bundle = data.stock('TEST')
            # A repeated scan at this candle boundary reuses the validated bundle.
            self.assertEqual(data.stock('TEST')['setup'], bundle['setup'])
            self.assertEqual(ticker.return_value.history.call_count, 2)
        self.assertEqual(len(bundle['setup']), 2)  # Today's daily candle is still open.
        self.assertEqual(bundle['setup'][-1]['end'], at('2026-11-27T13:00:00-05:00'))
        self.assertEqual(len(bundle['hourly']), 10)
        self.assertEqual(bundle['hourly'][-1]['end'], at('2026-11-30T10:30:00-05:00'))
        self.assertEqual(bundle['execution'][-1]['end'], at('2026-11-30T11:00:00-05:00'))
        self.assertIsNone(bundle['quote'])
        self.assertIn('Yahoo', bundle['source'])
        self.assertIn('entries blocked', bundle['quote_error'])

    def test_missing_session_or_intraday_bar_fails_closed(self):
        for missing in ('daily', 'intraday'):
            with self.subTest(missing=missing):
                data = self.data()
                data.calendar()
                daily, intraday = self.frames(data)
                if missing == 'daily':
                    daily = daily.drop(daily.index[1])
                else:
                    intraday = intraday.drop(intraday.index[2])
                with patch('yfinance.Ticker') as ticker:
                    ticker.return_value.history.side_effect = [daily, intraday]
                    ticker.return_value.history_metadata = {'currency':'USD', 'exchangeTimezoneName':'America/New_York'}
                    with self.assertRaisesRegex(ValueError, 'Missing'):
                        data.stock('TEST')

    def test_unknown_provider_rejected(self):
        config = runtime.default_config()
        config['stocks']['provider'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'provider'):
            runtime.validate_config(config)


if __name__ == '__main__':
    unittest.main()
