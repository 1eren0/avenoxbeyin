#!/usr/bin/env python3
"""recap --since/--until select an explicit range on local calendar days (#229); synthetic fixtures only.

How the local day is computed, with no daylight-saving arithmetic: a receipt stamp is one instant,
kept in UTC. Its local day is the calendar date that instant shows on the reader's clock
(``stamp.astimezone(zone).date()``), the rule daily/v3 file names follow since #149. A bare
``YYYY-MM-DD`` bound is compared with that date. No "midnight" instant is ever built, so a day on
which clocks change is simply 23 or 25 hours long and still belongs to one date.
"""
import argparse
from datetime import date, datetime, timedelta, timezone, tzinfo
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from v3_package_helpers import install, isolated_env, run_python, snapshot

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    import beyin_v3_projections as projections  # noqa: E402
    cli = _load('v3_recap_range_cli', ROOT / 'scripts/beyin_v3.py')
    entry = _load('v3_recap_range_entry', ROOT / 'scripts/beyin_entry.py')
finally:
    sys.dont_write_bytecode = _bytecode

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
DAY = date(2026, 9, 24)
# One minute inside and outside each edge of local day 2026-09-24 at UTC+3.
STAMPS = {
    'prev-2359': '2026-09-23T20:59:00+00:00',   # 23 Sep 23:59 local
    'own-offset': '2026-09-24T00:30:00+03:00',  # 24 Sep 00:30 local, written with its own offset
    'day-0001': '2026-09-23T21:01:00+00:00',    # 24 Sep 00:01 local
    'day-noon': '2026-09-24T09:00:00+00:00',    # 24 Sep 12:00 local
    'day-2359': '2026-09-24T20:59:00+00:00',    # 24 Sep 23:59 local
    'next-0001': '2026-09-24T21:01:00+00:00',   # 25 Sep 00:01 local
}


class ClockChange(tzinfo):
    """UTC+1 until 2026-03-29 01:00 UTC, UTC+2 from then on: local 29 March lasts 23 hours.

    Written out here so the test needs no zone database (Windows ships none)."""
    SWITCH = datetime(2026, 3, 29, 1, 0)

    def fromutc(self, moment):
        naive = moment.replace(tzinfo=None)
        return (naive + timedelta(hours=2 if naive >= self.SWITCH else 1)).replace(tzinfo=self)

    def utcoffset(self, moment):
        return timedelta(hours=2 if moment.replace(tzinfo=None) >= datetime(2026, 3, 29, 3, 0) else 1)

    def dst(self, moment):
        return self.utcoffset(moment) - timedelta(hours=1)

    def tzname(self, moment):
        return 'CLK'


class RecapRangeProjectionTest(unittest.TestCase):
    """The rule itself, with the reader's zone pinned in process so it also runs on Windows."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE receipts(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        for ident, stamp in STAMPS.items():
            self.add(ident, stamp)

    def add(self, ident, stamp):
        self.db.execute('INSERT INTO receipts VALUES (?,?)', (ident, json.dumps(
            {'event_id': ident, 'created_at': stamp, 'summary': ident, 'refs': ['notes/source.md']})))

    def recap(self, zone=PLUS3, **bounds):
        with patch.object(projections, '_local_zone', lambda: zone):
            return projections.recent_receipts(self.db, limit=100, **bounds)

    def names(self, result):
        return [item['summary'] for item in result['items']]

    def test_one_date_in_both_bounds_is_that_local_day_through_its_last_minute(self):
        result = self.recap(since=DAY, until=DAY)
        self.assertEqual(self.names(result), ['day-2359', 'day-noon', 'own-offset', 'day-0001'])
        self.assertEqual((result['from'], result['through'], result['timezone']), ('2026-09-24', '2026-09-24', 'local'))
        self.assertEqual({item['day'] for item in result['items']}, {'2026-09-24'})
        # created_at stays the UTC stamp; only the day follows the reader's clock.
        self.assertEqual(result['items'][-1]['created_at'], '2026-09-23T21:01:00+00:00')
        # The same receipts on a UTC clock: the day is the reader's, not an offset written in the stamp.
        self.assertEqual(self.names(self.recap(zone=UTC, since=DAY, until=DAY)), ['next-0001', 'day-2359', 'day-noon'])
        # Each selected receipt is one that receipt_day files under daily/v3/2026-09-24.md.
        self.assertEqual(sorted(self.names(result)),
                         sorted(name for name, stamp in STAMPS.items() if projections.receipt_day(stamp, PLUS3) == '2026-09-24'))

    def test_until_alone_has_no_lower_bound_and_from_says_so(self):
        self.add('ancient', '2019-01-01T00:00:00+00:00')
        result = self.recap(until=date(2026, 9, 23))
        self.assertEqual(self.names(result), ['prev-2359', 'ancient'])
        self.assertIsNone(result['from'])
        self.assertEqual(result['through'], '2026-09-23')

    def test_since_alone_ends_with_today(self):
        self.add('tomorrow', '2026-09-25T21:01:00+00:00')  # 26 Sep 00:01 local
        with patch.object(projections, '_local_zone', lambda: PLUS3):
            result = projections.recent_receipts(self.db, since=date(2026, 9, 25), today=date(2026, 9, 25))
        self.assertEqual(self.names(result), ['next-0001'])
        self.assertEqual((result['from'], result['through']), ('2026-09-25', '2026-09-25'))

    def test_timestamps_are_exact_inclusive_instants_and_mix_with_dates(self):
        noon, late = datetime(2026, 9, 24, 9, 0, tzinfo=UTC), datetime(2026, 9, 24, 20, 59, tzinfo=UTC)
        result = self.recap(since=noon, until=late)
        self.assertEqual(self.names(result), ['day-2359', 'day-noon'])
        self.assertEqual((result['from'], result['through']), ('2026-09-24', '2026-09-24'))
        self.assertEqual(self.names(self.recap(since=noon, until=late - timedelta(seconds=1))), ['day-noon'])
        self.assertEqual(self.names(self.recap(since=noon + timedelta(seconds=1), until=late)), ['day-2359'])
        self.assertEqual(self.names(self.recap(since=DAY, until=noon)), ['day-noon', 'own-offset', 'day-0001'])
        self.assertEqual(self.names(self.recap(since=late, until=DAY)), ['day-2359'])
        # 'through' is the local day of the instant: 21:01 UTC is already 25 Sep at UTC+3.
        self.assertEqual(self.recap(until=datetime(2026, 9, 24, 21, 1, tzinfo=UTC))['through'], '2026-09-25')

    def test_a_day_with_a_clock_change_is_still_one_whole_day(self):
        # Local 29 March runs from 28 March 23:00 UTC to 29 March 22:00 UTC: 23 hours. A window built
        # as "midnight plus 24 hours" would run an hour into 30 March and take 'next' with it.
        for ident, stamp in (('before', '2026-03-28T22:59:00+00:00'), ('first', '2026-03-28T23:01:00+00:00'),
                             ('after-gap', '2026-03-29T01:30:00+00:00'), ('last', '2026-03-29T21:59:00+00:00'),
                             ('next', '2026-03-29T22:01:00+00:00')):
            self.add(ident, stamp)
        day = date(2026, 3, 29)
        result = self.recap(zone=ClockChange(), since=day, until=day)
        self.assertEqual(self.names(result), ['last', 'after-gap', 'first'])
        self.assertEqual(self.names(self.recap(zone=ClockChange(), since=date(2026, 3, 30), until=date(2026, 3, 30))), ['next'])
        self.assertEqual(self.names(self.recap(zone=ClockChange(), since=date(2026, 3, 28), until=date(2026, 3, 28))), ['before'])

    def test_stamp_with_no_local_day_is_counted_never_given_an_invented_day(self):
        self.add('edge', '0001-01-01T00:00:00+00:00')  # one day west of UTC it has no calendar date
        result = self.recap(zone=timezone(timedelta(hours=-5)), until=DAY)
        self.assertNotIn('edge', self.names(result))
        self.assertEqual(result['undated_omitted'], 1)
        self.assertEqual(self.recap(until=DAY)['undated_omitted'], 0)  # east of UTC the same instant has a day

    def test_human_view_names_local_days_and_an_open_start(self):
        text = entry.human_result(self.recap(until=DAY), 'recap')
        self.assertEqual(text.splitlines()[0], 'Kaynakli etkinlik: baslangic siniri yok - 2026-09-24 '
                                              '(yerel gun; ajan kayitlari, bagimsiz dogrulanmis olgular degil)')
        self.assertIn('\n2026-09-24  day-0001\n', text)  # stamped 2026-09-23 in UTC
        self.assertNotIn('2026-09-23  day-0001', text)
        ranged = entry.human_result(self.recap(since=DAY, until=DAY), 'recap')
        self.assertTrue(ranged.startswith('Kaynakli etkinlik: 2026-09-24 - 2026-09-24 (yerel gun; '), ranged)
        # The --days window keeps its UTC header and UTC item dates.
        window = entry.human_result(projections.recent_receipts(self.db, today=date(2026, 9, 25)), 'recap')
        self.assertTrue(window.startswith('Kaynakli etkinlik: 2026-09-19 - 2026-09-25 (UTC; '), window)
        self.assertIn('\n2026-09-23  day-0001\n', window)


class RecapBoundArgumentTest(unittest.TestCase):
    def test_accepted_forms_are_a_bare_date_or_a_timestamp_with_zone(self):
        self.assertEqual(cli.recap_bound('2026-09-24'), DAY)
        self.assertNotIsInstance(cli.recap_bound('2026-09-24'), datetime)
        instant = datetime(2026, 9, 24, 15, 30, tzinfo=UTC)
        for text in ('2026-09-24T15:30:00Z', '2026-09-24T15:30Z', '2026-09-24T18:30:00+03:00',
                     '2026-09-24T10:30:00.000000-05:00', '2026-09-24T15:30:00+00:00'):
            with self.subTest(text=text):
                parsed = cli.recap_bound(text)
                self.assertEqual(parsed, instant)
                self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_everything_else_is_an_argument_error_on_every_python(self):
        # datetime.fromisoformat alone accepts several of these, and not the same set on 3.11 and 3.14.
        for text in ('nonsense', '', '2026-9-24', '20260924', '2026-W39-4', '2026-02-30', '24-09-2026',
                     '2026-09-24T12:00:00', '2026-09-24T12', '2026-09-24 12:00:00+03:00', '2026-09-24T24:00:00Z',
                     '2026-09-24T12:00:00+0300', '2026-09-24t12:00:00z', '2026-09-24T12:60:00Z',
                     '2026-09-24T12:00:00+25:00', '2026-09-24\n', ' 2026-09-24', '２０２６-09-24',
                     '0001-01-01T00:00:00+14:00'):
            with self.subTest(text=text):
                with self.assertRaises(argparse.ArgumentTypeError) as raised:
                    cli.recap_bound(text)
                self.assertIn('invalid date format: ' + repr(text), str(raised.exception))

    def test_order_check_compares_instants_or_local_days(self):
        at = lambda day, hour: datetime(2026, 9, day, hour, tzinfo=UTC)  # noqa: E731
        self.assertFalse(cli.recap_bounds_inverted(DAY, DAY))
        self.assertTrue(cli.recap_bounds_inverted(date(2026, 9, 25), DAY))
        self.assertFalse(cli.recap_bounds_inverted(at(24, 9), at(24, 9)))
        self.assertTrue(cli.recap_bounds_inverted(at(24, 9), at(24, 8)))
        # A date against an instant: these hold in every zone, UTC-12 through UTC+14.
        self.assertTrue(cli.recap_bounds_inverted(date(2026, 9, 26), at(24, 12)))
        self.assertTrue(cli.recap_bounds_inverted(at(26, 12), DAY))
        self.assertFalse(cli.recap_bounds_inverted(date(2026, 9, 23), at(24, 12)))
        self.assertFalse(cli.recap_bounds_inverted(at(24, 12), date(2026, 9, 26)))


TURKISH = 'Öğle: İstanbul ışığı, Şirket çağrısı'


class InstalledRecapRange:
    """The same cases through an installed vault's beyin.py; subclasses choose the zone it runs in."""
    TZ = None    # TZ for the installed commands; None keeps this machine's zone
    ZONE = None  # the same zone for building stamps; None reads this machine's clock

    @classmethod
    def at(cls, day, hour, minute):
        """UTC stamp of a wall-clock time on a calendar day in the zone the commands run in."""
        wall = datetime(day.year, day.month, day.day, hour, minute)
        aware = wall.replace(tzinfo=cls.ZONE) if cls.ZONE else wall.astimezone()
        return aware.astimezone(UTC).isoformat()

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix='v3-recap-range-')
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        cls.vault, cls.state = cls.root / 'vault', cls.root / 'state'
        (cls.vault / 'notes').mkdir(parents=True)
        (cls.vault / 'notes/source.md').write_text('# Source\n\nSynthetic evidence.\n', encoding='utf-8')
        cls.env = isolated_env(cls.root / 'home')
        zone = cls.TZ or os.environ.get('TZ')
        if zone:
            cls.env['TZ'] = zone
        installed = install(cls.vault, cls.state, cls.env)
        if installed.returncode:
            raise AssertionError(installed.stderr.decode('utf-8', errors='replace'))
        yesterday, tomorrow = DAY - timedelta(days=1), DAY + timedelta(days=1)
        cls.stamps = {'prev-2359': cls.at(yesterday, 23, 59), 'day-0001': cls.at(DAY, 0, 1), TURKISH: cls.at(DAY, 12, 0),
                      'day-2359': cls.at(DAY, 23, 59), 'next-0001': cls.at(tomorrow, 0, 1)}
        # Receipt sources as another device's sync delivers them: the stamp is the receipt's own.
        (cls.vault / 'receipts').mkdir()
        for summary, stamp in cls.stamps.items():
            event_id = 'range-' + summary
            metadata = {'kind': 'receipt', 'event_id': event_id, 'harness': 'codex', 'refs': ['notes/source.md'],
                        'visibility': 'internal', 'created_at': stamp}
            (cls.vault / 'receipts' / (hashlib.sha256(event_id.encode()).hexdigest() + '.md')).write_bytes(
                ('---\n' + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n---\n' + summary + '\n').encode('utf-8'))
        # And one written by the installed receipt command, stamped now.
        cls.today = datetime.now(UTC).astimezone(cls.ZONE).date()
        saved = run_python(cls.vault / 'beyin.py', ['receipt', '--harness', 'codex', '--event-id', 'range-now',
                                                   '--summary', 'written-now', '--ref', 'notes/source.md'], cls.vault, cls.env)
        if saved.returncode:
            raise AssertionError(saved.stderr.decode('utf-8', errors='replace'))

    def recap(self, *args):
        return run_python(self.vault / 'beyin.py', ['recap', *map(str, args)], self.vault, self.env)

    def result(self, *args):
        done = self.recap(*args, '--json')
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def human(self, *args):
        done = self.recap(*args, '--human')
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return done.stdout.decode('utf-8').splitlines()

    def names(self, result):
        return [item['summary'] for item in result['items']]

    def test_one_date_in_both_flags_returns_that_local_day_as_daily_v3_files_it(self):
        result = self.result('--since', DAY, '--until', DAY)
        self.assertEqual(self.names(result), ['day-2359', TURKISH, 'day-0001'])
        self.assertEqual((result['from'], result['through'], result['timezone']), ('2026-09-24', '2026-09-24', 'local'))
        self.assertEqual([item['created_at'] for item in result['items']],
                         [self.stamps[name] for name in ('day-2359', TURKISH, 'day-0001')])
        self.assertEqual({item['day'] for item in result['items']}, {'2026-09-24'})
        # recap synchronizes first, so the generated view of that day exists: it lists the same receipts.
        view = (self.vault / 'daily/v3/2026-09-24.md').read_bytes().decode('utf-8')
        self.assertEqual({name for name, stamp in self.stamps.items() if '## ' + stamp in view}, set(self.names(result)))
        lines = self.human('--since', DAY, '--until', DAY)
        self.assertEqual(lines[0], 'Kaynakli etkinlik: 2026-09-24 - 2026-09-24 '
                                   '(yerel gun; ajan kayitlari, bagimsiz dogrulanmis olgular degil)')
        self.assertEqual([line for line in lines if line[:4] == '2026'],
                         ['2026-09-24  day-2359', '2026-09-24  ' + TURKISH, '2026-09-24  day-0001'])

    def test_until_alone_has_no_lower_bound_and_from_says_so(self):
        result = self.result('--until', '2026-09-23')
        self.assertEqual(self.names(result), ['prev-2359'])
        self.assertIsNone(result['from'])
        self.assertEqual(result['through'], '2026-09-23')
        lines = self.human('--until', '2026-09-23')
        self.assertEqual(lines[0], 'Kaynakli etkinlik: baslangic siniri yok - 2026-09-23 '
                                   '(yerel gun; ajan kayitlari, bagimsiz dogrulanmis olgular degil)')
        self.assertEqual([line for line in lines if line[:4] == '2026'], ['2026-09-23  prev-2359'])

    def test_since_alone_runs_through_today_and_the_days_window_is_unchanged(self):
        result = self.result('--since', '2026-09-25')
        self.assertEqual(self.names(result), ['written-now', 'next-0001'])
        self.assertEqual(result['from'], '2026-09-25')
        self.assertIn(result['through'], (self.today.isoformat(), (self.today + timedelta(days=1)).isoformat()))
        self.assertEqual(self.names(self.result('--since', self.today)), ['written-now'])
        # Without the new flags the result has the shape it had: a UTC window and no per-item day.
        # Two days, so a run across UTC midnight still holds the receipt written a moment ago.
        window = self.result('--days', 2)
        self.assertEqual(window['timezone'], 'UTC')
        self.assertEqual((len(window['from']), len(window['through'])), (10, 10))
        self.assertEqual(self.names(window), ['written-now'])
        self.assertEqual(sorted(window['items'][0]), ['created_at', 'refs', 'source', 'summary'])

    def test_timestamps_are_exact_inclusive_bounds_with_an_offset_or_z(self):
        noon = datetime.fromisoformat(self.stamps[TURKISH])
        since = noon.astimezone(timezone(timedelta(hours=-5))).isoformat()  # the same instant, another offset
        until = self.stamps['next-0001'].replace('+00:00', 'Z')
        result = self.result('--since', since, '--until', until)
        self.assertEqual(self.names(result), ['next-0001', 'day-2359', TURKISH])
        self.assertEqual((result['from'], result['through']), ('2026-09-24', '2026-09-25'))
        just_after = (noon + timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')
        self.assertEqual(self.names(self.result('--since', just_after, '--until', DAY)), ['day-2359'])
        self.assertEqual(self.names(self.result('--since', DAY, '--until', since)), [TURKISH, 'day-0001'])

    def test_usage_errors_exit_2_on_stderr_with_empty_stdout_and_touch_nothing(self):
        zoneless = 'argument --since: invalid date format: ' + repr('2026-09-24T12:00:00')
        combined = 'argument --days: not allowed with argument --since/--until'
        inverted = 'argument --since: must not be after --until'
        cases = (
            (('--since', 'nonsense', '--json'), "argument --since: invalid date format: 'nonsense'"),
            (('--since', 'nonsense', '--human'), "argument --since: invalid date format: 'nonsense'"),
            (('--until', '2026-02-30', '--human'), "argument --until: invalid date format: '2026-02-30'"),
            (('--since', '2026-09-24T12:00:00', '--json'), zoneless),
            (('--days', 3, '--since', DAY, '--human'), combined),
            (('--days', 3, '--until', DAY, '--json'), combined),
            (('--since', '2026-09-25', '--until', DAY, '--json'), inverted),
            # 26 Sep 12:00 UTC is 26 or 27 Sep on every clock, after the whole of 24 Sep.
            (('--since', '2026-09-26T12:00:00Z', '--until', DAY, '--human'), inverted),
        )
        before = snapshot(self.vault), snapshot(self.state)
        for args, message in cases:
            with self.subTest(args=args):
                done = self.recap(*args)
                self.assertEqual(done.returncode, 2, done.stderr.decode('utf-8', errors='replace'))
                self.assertEqual(done.stdout, b'')
                self.assertIn(message, done.stderr.decode('utf-8', errors='replace'))
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)


class InstalledRecapRangeTest(InstalledRecapRange, unittest.TestCase):
    """This machine's own zone: runs everywhere, Windows included."""


@unittest.skipUnless(hasattr(time, 'tzset'), 'TZ selects the process zone on POSIX only')
class InstalledRecapRangeEastOfUtcTest(InstalledRecapRange, unittest.TestCase):
    """Three hours east of UTC, where the local day and the UTC day differ for three hours every night."""
    TZ = 'RCP-3'  # POSIX form (name, then hours to add to reach UTC); needs no zone database
    ZONE = PLUS3


if __name__ == '__main__':
    unittest.main()
