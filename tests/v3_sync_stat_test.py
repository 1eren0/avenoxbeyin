#!/usr/bin/env python3
"""Stat signature cache of warm syncs (#233): no lost edit, every conflict stays visible.

Every case is checked against LegacySyncEngine, the frozen engine that reads every source,
on its own state directory over the same vault. A cache hit must give the same sync report
and the same events, records and ownership rows as reading the bytes, and every indexed
record must carry the hash of the bytes now on disk.

Fixtures are written and read as bytes: text mode would turn '\n' into '\r\n' on Windows,
and the expected texts below are LF.
"""
from contextlib import contextmanager
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env
from v3_sync_test import load_module
from v3_sync_warm_test import LegacySyncEngine

subject = load_module()
SCRIPTS = Path(subject.__file__).resolve().parent
CLI = Path(__file__).resolve().parents[1] / 'scripts/beyin_v3.py'
_real_time_ns = time.time_ns
NO_CHANGE_TIME = 'Windows os.stat has no change time: st_ctime is the creation time'


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))
    return path


def get(path):
    return path.read_bytes().decode('utf-8')


def coarse_signature(st):
    """A filesystem whose stat cannot see an in-place rewrite: 2 s mtime ticks, no ctime or
    file id (FAT; on Windows st_ctime is the creation time)."""
    return (st.st_size, st.st_mtime_ns // 2_000_000_000 * 2_000_000_000, 0, '0')


def weak_signature(st):
    """The signature first proposed for the cache: size and mtime only."""
    return (st.st_size, st.st_mtime_ns, 0, '0')


def tunneled_signature(st):
    """NTFS hands the creation time of a replaced file to the file that takes its name
    (tunneling), so there an atomic save shows only in the file id."""
    return (st.st_size, st.st_mtime_ns, 0, str(st.st_ino))


@contextmanager
def clock(seconds):
    """Run a sync as if it happened `seconds` from now (no real sleeping)."""
    offset = int(seconds * 1_000_000_000)
    with patch.object(subject.time, 'time_ns', side_effect=lambda: _real_time_ns() + offset):
        yield


@contextmanager
def clock_at(time_ns):
    """Run a sync at exactly this time, however long the test itself takes."""
    with patch.object(subject.time, 'time_ns', return_value=time_ns):
        yield


class StatSignatureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-sync-stat-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.engine = subject.SyncEngine(self.vault, self.state)
        self.oracle = LegacySyncEngine(self.vault, self.root / 'oracle-state')
        self.minutes = 0
        self.last_reads = []

    def write(self, name='notes/note.md', id='note', text='Alpha calibration.\n', **fields):
        return put(self.vault / name, subject.render(dict(id=id, kind='note', revision=1, **fields), text))

    def snapshot(self, engine):
        with engine.store._connect() as db:
            return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                    for table in ('events', 'records', 'markdown_sources')}

    def later(self):
        """Each settled sync happens a minute after the previous one, past the racy window."""
        self.minutes += 1
        return clock(60 * self.minutes)

    @contextmanager
    def reads(self):
        calls = []
        original = Path.read_bytes

        def counted(path):
            calls.append(Path(path).name)
            return original(path)

        with patch.object(Path, 'read_bytes', counted):
            yield calls

    def sync_both(self):
        """One sync of each engine over the same vault; reports and tables must be equal.

        self.last_reads names the sources the cached engine read (twice each: scan and validation).
        """
        with self.later():
            with self.reads() as calls:
                result = self.engine.sync()
            expected = self.oracle.sync()
        self.last_reads = calls
        self.assertEqual(result, expected)
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
        self.assert_index_is_fresh()
        return result

    def assert_index_is_fresh(self):
        """Retrieval's own gate: a record is used only while its hash is that of its source."""
        with self.engine.store._connect() as db:
            rows = db.execute('SELECT s.source, r.payload FROM markdown_sources s JOIN records r ON r.id=s.id').fetchall()
        for source, payload in rows:
            # open(), not Path.read_bytes: the tests patch and count that one.
            with open(self.vault / source, 'rb') as handle:
                self.assertEqual(json.loads(payload)['source_sha256'], hashlib.sha256(handle.read()).hexdigest(), source)

    def signatures(self):
        """source -> (payload_sha256, seen_ns); an empty hash is a signature seen once, not trusted."""
        with self.engine.store._connect() as db:
            return {row[0]: row[1:] for row in db.execute('SELECT source, payload_sha256, seen_ns FROM source_signatures')}

    def settle(self):
        """Index the vault and make every signature trusted (read after the racy window)."""
        self.sync_both()
        self.sync_both()

    def record(self, id):
        with self.engine.store._connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def rewrite_same_size(self, path, old, new):
        raw = path.read_bytes()
        self.assertEqual(len(old), len(new))
        path.write_bytes(raw.replace(old, new))

    def test_settled_unchanged_sources_are_not_read(self):
        for index in range(5):
            self.write(f'notes/note-{index}.md', id=f'note-{index}')
        self.settle()
        with self.reads() as calls:
            result = self.sync_both()
        self.assertEqual(result['status'], 'succeeded')
        # Only the oracle read the five notes (twice each: scan and validation).
        self.assertEqual(len(calls), 10)
        self.assertEqual(self.last_reads, [])
        with self.reads() as calls, self.later():
            self.assertEqual(self.engine.sync()['indexed'], 5)
        self.assertEqual(calls, [])

    def test_a_signature_is_trusted_from_its_second_read(self):
        # Seen once: remembered, not trusted. Read again two seconds later: trusted.
        self.write()
        self.sync_both()
        (payload, seen), = self.signatures().values()
        self.assertEqual(payload, '')
        self.sync_both()
        self.assertEqual(self.last_reads, ['note.md', 'note.md'])
        (payload, again), = self.signatures().values()
        self.assertEqual((len(payload), again), (64, seen))
        self.sync_both()
        self.assertEqual(self.last_reads, [])

    def test_a_same_size_edit_is_indexed(self):
        path = self.write()
        other = self.write('notes/other.md', id='other')
        self.settle()
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        with self.reads() as calls, self.later():
            result = self.engine.sync()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
        self.assertEqual(self.record('note')['revision'], 2)
        self.assertIn('note.md', calls)
        self.assertNotIn(other.name, calls)
        self.assertEqual(result['status'], 'succeeded')
        with self.later():
            self.assertEqual(self.oracle.sync(), result)
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))

    @unittest.skipIf(os.name == 'nt', NO_CHANGE_TIME)
    def test_an_edit_with_its_mtime_put_back_is_indexed(self):
        path = self.write()
        self.settle()
        before = path.stat()
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    def test_a_second_write_in_the_same_timestamp_tick_is_indexed(self):
        # Coarse timestamps: both writes carry one mtime. The first was read within the
        # racy window, so its signature is not trusted and the second write is read.
        path = self.write()
        with patch.object(subject, '_stat_signature', coarse_signature):
            with clock(0.5):
                self.engine.sync()
                self.oracle.sync()
            before = path.stat()
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with clock(1):
                result = self.engine.sync()
                self.assertEqual(result, self.oracle.sync())
            self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
            # Read after the window, the signature is trusted from then on.
            with clock(10):
                self.engine.sync()
            with self.reads() as calls, clock(20):
                self.engine.sync()
            self.assertEqual(calls, [])

    def test_a_replaced_file_is_indexed(self):
        # Atomic save: a new file with the same size and mtime takes the name.
        path = self.write()
        self.settle()
        before = path.stat()
        replacement = path.with_name('note.md.tmp')
        replacement.write_bytes(path.read_bytes().replace(b'Alpha', b'Omega'))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        self.assertNotEqual(path.stat().st_ino, before.st_ino)
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    def test_a_replaced_file_is_known_by_its_file_id_alone(self):
        path = self.write()
        with patch.object(subject, '_stat_signature', tunneled_signature):
            self.settle()
            before = path.stat()
            replacement = path.with_name('note.md.tmp')
            replacement.write_bytes(path.read_bytes().replace(b'Alpha', b'Omega'))
            self.put_back(replacement, before)
            os.replace(replacement, path)
            self.sync_both()
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    def test_the_first_proposed_signature_misses_these_edits(self):
        # Size and mtime alone, trusted at once: why the signature and the waits above exist.
        path = self.write()
        with patch.object(subject, '_stat_signature', weak_signature):
            self.settle()
            before = path.stat()
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            self.put_back(path, before)
            with self.later():
                self.engine.sync()
            self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')
            self.assertEqual(self.engine.store._eligible()[1], 1)

    def test_a_lost_ownership_row_is_restored(self):
        # A hit needs markdown_sources to give the id to this source; without the row a full
        # read adopts the record again, so the cached sync must read too.
        self.write()
        self.settle()
        for engine in (self.engine, self.oracle):
            with engine.store._connect() as db:
                db.execute("DELETE FROM markdown_sources WHERE id='note'")
        self.sync_both()
        self.assertEqual(self.last_reads, ['note.md', 'note.md'])
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute('SELECT id, source FROM markdown_sources').fetchall(), [('note', 'notes/note.md')])

    def test_a_clock_set_back_is_indexed(self):
        path = self.write()
        self.settle()
        before = path.stat()
        put(path, get(path).replace('Alpha', 'Earlier clock'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns - 3600 * 1_000_000_000))
        with clock(-3600):
            result = self.engine.sync()
            self.assertEqual(result, self.oracle.sync())
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
        self.assertEqual(self.record('note')['text'], 'Earlier clock calibration.\n')
        self.sync_both()

    def test_an_unreadable_placeholder_keeps_its_record_without_a_read(self):
        path = self.write()
        self.settle()
        original = Path.read_bytes

        def evicted(file):
            if Path(file).name == path.name:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original(file)

        with patch.object(Path, 'read_bytes', evicted), self.later():
            result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_type='delete'").fetchone()[0], 0)

    def test_a_changed_unreadable_placeholder_matches_a_full_read(self):
        # Same as reading every source: the warning, and what happens to the record.
        path = self.write()
        self.settle()
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
        original = Path.read_bytes

        def evicted(file):
            if Path(file).name == path.name:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original(file)

        with patch.object(Path, 'read_bytes', evicted):
            result = self.sync_both()
        self.assertEqual(result['warnings'], [{'source': 'notes/note.md', 'reason': str(OSError(errno.EDEADLK, 'Resource deadlock avoided'))}])
        # Readable again: indexed again, as in a full read.
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')

    def test_duplicate_ids_stay_quarantined(self):
        self.write()
        self.settle()
        self.write('notes/copy.md')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['conflicts'], [{'id': 'note', 'reason': 'duplicate source id; all copies quarantined'}])
            self.assertIsNone(self.record('note'))
        (self.vault / 'notes/copy.md').unlink()
        self.assertEqual(self.sync_both()['status'], 'succeeded')
        self.assertIsNotNone(self.record('note'))

    def test_conflict_markers_stay_visible(self):
        path = self.write()
        self.settle()
        put(path, get(path) + '<<<<<<< ours\nA\n=======\nB\n>>>>>>> theirs\n')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['warnings'], [{'source': 'notes/note.md',
                                                   'reason': 'unresolved git conflict markers; resolve the merge before sync'}])

    def test_id_owned_by_another_writer_stays_a_conflict(self):
        payload = subject._json(dict(id='foreign', revision=7, source='notes/note.md', text='Other writer', kind='note'))
        for engine in (self.engine, self.oracle):
            with engine.store._connect() as db:
                db.execute('INSERT INTO records VALUES (?,?)', ('foreign', payload))
        self.write()
        self.write('notes/collision.md', id='foreign')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['conflicts'], [{'id': 'foreign', 'reason': 'id already owned by another record'}])

    def test_a_manual_receipt_view_edit_stays_a_conflict(self):
        self.write()
        self.settle()
        self.engine.receipt('event-1', 'Calibrated alpha.', ['notes/note.md'], 'codex')
        views = [path for path in self.vault.rglob('*.md') if 'v3' in path.parts]
        self.assertTrue(views)
        view = views[0]
        put(view, get(view) + '\nEdited by hand.\n')
        relative = view.relative_to(self.vault).as_posix()
        for _ in range(3):
            with self.later():
                result = self.engine.sync()
            self.assertIn({'source': relative, 'reason': 'manual receipt view edit preserved'}, result['conflicts'])
            self.assertTrue(get(view).endswith('Edited by hand.\n'))

    def test_outside_vault_symlink_is_rejected_on_every_sync(self):
        self.write()
        outside = self.root / 'outside'
        outside.mkdir()
        put(outside / 'outside.md', 'Outside source.\n')
        try:
            (self.vault / 'outside-link.md').symlink_to(outside / 'outside.md')
            (self.vault / 'broken-link.md').symlink_to(outside / 'missing.md')
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable on this platform')
        expected = [{'source': 'broken-link.md', 'reason': 'source outside vault'},
                    {'source': 'outside-link.md', 'reason': 'source outside vault'}]
        for _ in range(3):
            self.assertEqual(self.sync_both()['warnings'], expected)

    def test_an_index_written_by_a_version_without_signatures_is_not_trusted(self):
        # Rollback to a version without the table: it re-indexes an edit, the edit is then
        # reverted with its old mtime, and the update comes back. Stat cannot see the revert
        # here; the indexed payload is no longer the one the signature was recorded for.
        path = self.write()
        with patch.object(subject, '_stat_signature', coarse_signature):
            self.settle()
            original = path.read_bytes()
            before = path.stat()
            rolled_back = LegacySyncEngine(self.vault, self.state)
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.later():
                rolled_back.sync()
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
            path.write_bytes(original)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.reads() as calls, self.later():
                self.engine.sync()
            self.assertIn('note.md', calls)
            self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')

    def test_a_payload_rewritten_by_another_writer_is_rebuilt(self):
        # The store API can rewrite an indexed row without touching its source; a full read
        # rebuilds the row from the source, so a hit must not keep the rewritten payload.
        self.write()
        self.settle()
        for engine in (self.engine, self.oracle):
            engine.store.update_task('note', 1, {'status': 'rewritten elsewhere'})
        self.sync_both()
        self.assertNotIn('status', self.record('note'))

    def test_rollback_edits_are_indexed_after_update(self):
        path = self.write()
        self.settle()
        rolled_back = LegacySyncEngine(self.vault, self.state)
        put(path, get(path).replace('Alpha', 'Rolled back'))
        with self.later():
            rolled_back.sync()
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Rolled back calibration.\n')
        self.sync_both()

    def test_new_code_trusts_no_stored_signature(self):
        self.write()
        self.settle()
        with self.reads() as calls, self.later(), patch.object(subject, '_signature_epoch', return_value='other code'):
            self.engine.sync()
        self.assertIn('note.md', calls)
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='source_signature_epoch'").fetchone()[0], 'other code')

    def test_signature_includes_ctime_and_file_id(self):
        st = os.stat(self.write())
        self.assertEqual(subject._stat_signature(st), (st.st_size, st.st_mtime_ns, st.st_ctime_ns, str(st.st_ino)))

    def put_back(self, path, before):
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))

    def test_a_file_server_clock_behind_this_one_does_not_hide_an_edit(self):
        # Timestamps stamped by a clock 10 s behind this machine's, in 2 s ticks: the first write
        # already looks old enough, and the edit lands in its tick. A signature seen once is not
        # trusted; it settles on a read two seconds of this machine's own clock later.
        start, second = time.time_ns(), 1_000_000_000
        path = self.write()
        with patch.object(subject, '_stat_signature', coarse_signature):
            for seconds, old, new in ((10, None, None), (10.5, b'Alpha', b'Omega'), (11.5, b'Omega', b'Gamma')):
                if old:
                    before = path.stat()
                    self.rewrite_same_size(path, old, new)
                    self.put_back(path, before)
                # The sync times are pinned: a slow test run must not let the two seconds pass.
                with clock_at(start + int(seconds * second)):
                    result = self.engine.sync()
                    self.assertEqual(result, self.oracle.sync())
                self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
                self.assert_index_is_fresh()
            self.assertEqual(self.record('note')['text'], 'Gamma calibration.\n')
            with clock_at(start + 3600 * second):
                self.engine.sync()
            with self.reads() as calls, clock_at(start + 7200 * second):
                self.engine.sync()
            self.assertEqual(calls, [])

    def test_a_clock_set_back_after_a_sighting_starts_the_wait_over(self):
        # A first sighting dated ahead of the clock cannot prove that two seconds went by.
        self.write()
        with clock(3600):
            self.engine.sync()
        (payload, seen), = self.signatures().values()
        with clock(5):
            self.engine.sync()
        (payload, again), = self.signatures().values()
        self.assertEqual(payload, '')
        self.assertLess(again, seen)
        with clock(10):
            self.engine.sync()
        with self.reads() as calls, clock(20):
            self.engine.sync()
        self.assertEqual(calls, [])

    def test_a_backup_restored_over_an_edit_is_indexed(self):
        # cp -p: the old bytes and the old mtime come back in place.
        path = self.write()
        self.settle()
        backup = self.root / 'backup.md'
        shutil.copy2(path, backup)
        put(path, get(path).replace('Alpha', 'A much longer edit of the'))
        self.settle()
        shutil.copy2(backup, path)
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')
        self.sync_both()

    @unittest.skipIf(os.name == 'nt', NO_CHANGE_TIME)
    def test_a_backup_restored_over_an_edit_of_one_size_and_mtime_is_indexed(self):
        path = self.write()
        self.settle()
        before = path.stat()
        backup = self.root / 'backup.md'
        shutil.copy2(path, backup)
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        self.put_back(path, before)
        self.settle()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
        shutil.copy2(backup, path)
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')

    def test_renames_are_followed(self):
        labelled = self.write('notes/labelled.md', id='labelled')
        plain = put(self.vault / 'notes/plain.md', 'A note without frontmatter.\n')
        self.settle()
        with self.engine.store._connect() as db:
            plain_id, = db.execute("SELECT id FROM markdown_sources WHERE source='notes/plain.md'").fetchone()
        os.rename(labelled, labelled.with_name('moved.md'))
        os.rename(plain, plain.with_name('plain-moved.md'))
        self.assertEqual(self.sync_both()['deleted'], 1)
        self.assertEqual(self.record('labelled')['source'], 'notes/moved.md')
        self.assertIsNone(self.record(plain_id))
        self.assertEqual(set(self.signatures()), {'notes/moved.md', 'notes/plain-moved.md'})
        self.settle()
        # A directory rename changes no stat field of the files below it.
        os.rename(self.vault / 'notes', self.vault / 'archive')
        self.sync_both()
        self.assertEqual(sorted(set(self.last_reads)), ['moved.md', 'plain-moved.md'])
        self.assertEqual(self.record('labelled')['source'], 'archive/moved.md')
        self.assertEqual(set(self.signatures()), {'archive/moved.md', 'archive/plain-moved.md'})
        self.settle()
        self.sync_both()
        self.assertEqual(self.last_reads, [])

    def test_a_case_only_rename_is_followed(self):
        # One file under a new spelling on a case-insensitive volume, a plain rename elsewhere.
        path = self.write('notes/Note.md')
        self.settle()
        os.rename(path, path.with_name('note.md'))
        self.sync_both()
        self.assertEqual(self.record('note')['source'], 'notes/note.md')
        self.assertEqual(set(self.signatures()), {'notes/note.md'})
        self.settle()

    def test_a_note_moved_out_and_back_is_read_again(self):
        # While it was away the walk did not reach it: its record and its signature left with it,
        # so an edit that no stat field shows is still read when it returns.
        path = self.write()
        self.write('notes/other.md', id='other')
        with patch.object(subject, '_stat_signature', coarse_signature):
            self.settle()
            before = path.stat()
            away = self.root / 'away.md'
            os.rename(path, away)
            self.assertEqual(self.sync_both()['deleted'], 1)
            self.assertEqual(set(self.signatures()), {'notes/other.md'})
            self.rewrite_same_size(away, b'Alpha', b'Omega')
            self.put_back(away, before)
            os.rename(away, path)
            self.sync_both()
            self.assertEqual(sorted(set(self.last_reads)), ['note.md'])
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    @unittest.skipIf(os.name == 'nt', NO_CHANGE_TIME)
    def test_a_note_edited_outside_the_vault_between_two_syncs_is_indexed(self):
        path = self.write()
        self.settle()
        before = path.stat()
        away = self.root / 'away.md'
        os.rename(path, away)
        self.rewrite_same_size(away, b'Alpha', b'Omega')
        self.put_back(away, before)
        os.rename(away, path)
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    @unittest.skipIf(os.name == 'nt', 'not run on NTFS: a hard link is reasoned about in the review, not asserted')
    def test_hard_links_are_two_sources_of_one_file(self):
        first = put(self.vault / 'notes/first.md', 'Alpha calibration.\n')
        second = self.vault / 'notes/second.md'
        try:
            os.link(first, second)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('hard links unavailable on this filesystem')
        self.settle()
        self.sync_both()
        self.assertEqual(self.last_reads, [])
        self.rewrite_same_size(first, b'Alpha', b'Omega')
        self.assertEqual(self.sync_both()['indexed'], 2)
        self.assertEqual(sorted(set(self.last_reads)), ['first.md', 'second.md'])
        with self.engine.store._connect() as db:
            texts = [json.loads(row[0])['text'] for row in db.execute('SELECT payload FROM records')]
        self.assertEqual(texts, ['Omega calibration.\n'] * 2)

    def test_a_note_replaced_by_a_symlink_is_rejected_on_every_sync(self):
        path = self.write()
        target = self.write('notes/target.md', id='target')
        self.settle()
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable on this platform')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['warnings'], [{'source': 'notes/note.md', 'reason': 'symlink source rejected'}])
            self.assertIsNone(self.record('note'))
            self.assertIsNotNone(self.record('target'))
        self.assertEqual(set(self.signatures()), {'notes/target.md'})

    def test_a_source_dated_in_the_future_is_read_every_time(self):
        # A note stamped ahead of the clock (another device, a wrong date) never settles.
        path = self.write()
        self.write('notes/other.md', id='other')
        future = time.time_ns() + 86_400 * 1_000_000_000
        os.utime(path, ns=(future, future))
        self.settle()
        for old, new in ((b'Alpha', b'Omega'), (b'Omega', b'Gamma')):
            self.sync_both()
            self.assertEqual(sorted(set(self.last_reads)), ['note.md'])
            self.assertEqual(self.signatures()['notes/note.md'][0], '')
            self.rewrite_same_size(path, old, new)
            os.utime(path, ns=(future, future))
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Gamma calibration.\n')

    def test_a_directory_that_becomes_excluded_takes_its_signatures_with_it(self):
        # node_modules is the exclusion the walk has today; a configured name prunes the same
        # walk. Excluded: the records and signature rows below it leave. Included again: the
        # files are read, even when no stat field of theirs moved.
        path = self.write('notes/pkg/note.md')
        self.write('notes/pkg/deep/inner.md', id='inner')
        self.write('notes/keep.md', id='keep')
        with patch.object(subject, '_stat_signature', coarse_signature):
            self.settle()
            self.assertEqual(set(self.signatures()), {'notes/keep.md', 'notes/pkg/note.md', 'notes/pkg/deep/inner.md'})
            before = path.stat()
            os.rename(self.vault / 'notes/pkg', self.vault / 'notes/node_modules')
            result = self.sync_both()
            self.assertEqual((result['indexed'], result['deleted']), (1, 2))
            self.assertEqual(set(self.signatures()), {'notes/keep.md'})
            self.assertEqual(self.last_reads, [])
            hidden = self.vault / 'notes/node_modules/note.md'
            self.rewrite_same_size(hidden, b'Alpha', b'Omega')
            self.put_back(hidden, before)
            for _ in range(2):
                self.sync_both()
            os.rename(self.vault / 'notes/node_modules', self.vault / 'notes/pkg')
            self.assertEqual(self.sync_both()['indexed'], 3)
            self.assertEqual(sorted(set(self.last_reads)), ['inner.md', 'note.md'])
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
            self.settle()
            self.sync_both()
            self.assertEqual(self.last_reads, [])

    def test_read_time_diagnostics_are_reported_on_every_warm_sync(self):
        # A source that only warns is never indexed, so it has no signature and is read each time.
        self.write('notes/good.md', id='good')
        self.write('notes/dead.md', id='dead', supersedes=['no-such-record'])
        self.write('notes/generated.md', id='generated', generated=True)
        self.write('notes/visibility.md', id='visibility', visibility='secret')
        put(self.vault / 'notes/yaml.md', '---\nid: yaml\nnested: {a: 1}\n---\nBody.\n')
        put(self.vault / 'notes/task.md', '---\nid: task\nkind: task\n---\n---\nid: inner\n---\nBody.\n')
        put(self.vault / 'notes/open.md', '---\nid: open\nBody.\n')
        (self.vault / 'notes/binary.md').write_bytes(b'\xff\xfe\x00not utf-8')
        first = self.sync_both()
        self.assertEqual(first['status'], 'degraded')
        self.assertEqual(sorted(warning['source'] for warning in first['warnings']),
                         ['notes/binary.md', 'notes/open.md', 'notes/task.md', 'notes/visibility.md', 'notes/yaml.md'])
        self.assertEqual((first['indexed'], first['supersedes_issue_count']), (2, 1))
        for _ in range(4):
            self.assertEqual(self.sync_both(), first)
        self.assertEqual(sorted(set(self.last_reads)),
                         ['binary.md', 'generated.md', 'open.md', 'task.md', 'visibility.md', 'yaml.md'])
        self.assertEqual(set(self.signatures()), {'notes/good.md', 'notes/dead.md'})

    def test_a_receipt_divergence_is_reported_on_every_warm_sync(self):
        # #205/#239: another device wrote the same event_id and the merge kept its file.
        self.write('notes/task.md', id='task')
        other = self.root / 'other-vault'
        put(other / 'notes/task.md', get(self.vault / 'notes/task.md'))
        there = subject.SyncEngine(other, self.root / 'other-state')
        mine = self.engine.receipt('shared-topic', 'Calibration done here.', ['notes/task.md'], 'codex')
        there.receipt('shared-topic', 'Calibration done there.', ['notes/task.md'], 'claude')
        source = self.vault / mine['source']
        source.unlink()
        source.write_bytes((other / mine['source']).read_bytes())
        for _ in range(4):
            with self.later(), self.reads() as calls:
                result = self.engine.sync()
            self.assertEqual(result['status'], 'conflict')
            self.assertEqual([conflict['source'] for conflict in result['conflicts']
                              if conflict.get('kind') == 'receipt_divergence'], [mine['source']])
        self.assertNotIn('task.md', calls)

    def test_a_task_written_through_the_engine_is_read_back(self):
        # The write path replaces the source and syncs in one call, inside the racy window.
        put(self.vault / 'notes/task.md', subject.render(dict(id='task', kind='task', revision=1, status='active'), 'Calibrate.\n'))
        self.settle()
        with self.later():
            updated = self.engine.update_task('task', 1, {'status': 'waiting'})
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(self.signatures()['notes/task.md'][0], '')
        with self.later():
            self.oracle.sync()
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
        self.settle()
        self.assertEqual(self.record('task')['status'], 'waiting')
        self.sync_both()
        self.assertEqual(self.last_reads, [])

    def test_a_missing_or_foreign_signature_table_is_rebuilt(self):
        # Under an unchanged code version: a tool dropped the table, or left one of another shape.
        self.write()
        for statements in (('DROP TABLE source_signatures',),
                           ('DROP TABLE source_signatures',
                            'CREATE TABLE source_signatures(source TEXT PRIMARY KEY, id TEXT NOT NULL)',
                            "INSERT INTO source_signatures VALUES ('notes/note.md','note')")):
            self.settle()
            with self.engine.store._connect() as db:
                for statement in statements:
                    db.execute(statement)
            self.sync_both()
            self.assertEqual(self.last_reads, ['note.md', 'note.md'])
            self.assertEqual(self.signatures()['notes/note.md'][0], '')

    def test_signatures_are_saved_without_sqlite_json_functions(self):
        # The row-by-row fallback of save(), for an SQLite built without json_each.
        class WithoutJson:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *parameters):
                if 'json_each' in sql:
                    raise sqlite3.OperationalError('no such table: json_each')
                return self.db.execute(sql, *parameters)

            def executemany(self, sql, rows):
                return self.db.executemany(sql, rows)

        cache = subject._SignatureCache
        self.write()
        gone = self.write('notes/gone.md', id='gone')
        with patch.object(subject, '_SignatureCache', lambda db, *args, **kwargs: cache(WithoutJson(db), *args, **kwargs)):
            self.settle()
            self.assertEqual({source: len(row[0]) for source, row in self.signatures().items()},
                             {'notes/note.md': 64, 'notes/gone.md': 64})
            gone.unlink()
            self.assertEqual(self.sync_both()['deleted'], 1)
            self.assertEqual(self.last_reads, [])
            self.assertEqual(set(self.signatures()), {'notes/note.md'})

    def test_a_sync_that_fails_before_commit_leaves_no_signature_behind(self):
        # Records and signatures are one transaction: neither survives without the other.
        path = self.write()
        self.settle()
        settled = self.signatures()
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        before = self.snapshot(self.engine)
        with patch.object(subject, 'project_receipts', side_effect=RuntimeError('crash after the signatures')), self.later():
            with self.assertRaises(RuntimeError):
                self.engine.sync()
        self.assertEqual(self.snapshot(self.engine), before)
        self.assertEqual(self.signatures(), settled)
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    def test_two_writers_of_one_state_share_the_signatures(self):
        # SQLite's write lock serializes whole syncs, so each one sees the other's finished rows.
        paths = [self.write(f'notes/note-{index}.md', id=f'note-{index}') for index in range(6)]
        second = subject.SyncEngine(self.vault, self.state)
        self.settle()
        self.rewrite_same_size(paths[0], b'Alpha', b'Omega')
        errors = []

        def run(engine):
            try:
                for _ in range(4):
                    engine.sync()
            except Exception as exc:  # reported below, in the test's thread
                errors.append(exc)

        with self.later():
            threads = [threading.Thread(target=run, args=(engine,)) for engine in (self.engine, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.oracle.sync()
        self.assertEqual(errors, [])
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
        self.settle()
        with self.reads() as calls, self.later():
            self.assertEqual(second.sync()['indexed'], 6)
        self.assertEqual(calls, [])

    def test_a_vault_and_state_copied_from_another_machine_are_read_again(self):
        # Same paths, bytes and mtimes, other files: no signature of the first machine matches.
        for index in range(3):
            self.write(f'notes/note-{index}.md', id=f'note-{index}')
        self.settle()
        elsewhere = self.root / 'first-machine'
        os.rename(self.vault, elsewhere)
        shutil.copytree(elsewhere, self.vault)
        changed = self.vault / 'notes/note-0.md'
        before = changed.stat()
        self.rewrite_same_size(changed, b'Alpha', b'Omega')
        self.put_back(changed, before)
        self.sync_both()
        self.assertEqual(sorted(set(self.last_reads)), ['note-0.md', 'note-1.md', 'note-2.md'])
        self.assertEqual(self.record('note-0')['text'], 'Omega calibration.\n')

    def test_a_full_sync_reads_what_no_stat_can_show(self):
        # The limit of any stat cache: an in-place edit of the same size with its mtime put back,
        # on a filesystem without a change time (FAT, exFAT; os.stat on Windows). A normal sync
        # cannot see it; retrieval still refuses the record (its hash is not the source's), and
        # `sync --full` reads everything.
        path = self.write()
        self.write('notes/other.md', id='other')
        with patch.object(subject, '_stat_signature', coarse_signature):
            self.settle()
            before = path.stat()
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            self.put_back(path, before)
            with self.reads() as calls, self.later():
                self.engine.sync()
            self.assertEqual(calls, [])
            self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')
            self.assertEqual(self.engine.store._eligible()[1], 1)
            with self.later():
                self.oracle.sync()
            with self.reads() as calls, self.later():
                result = self.engine.sync(full=True)
            self.assertEqual(sorted(set(calls)), ['note.md', 'other.md'])
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
            self.assert_index_is_fresh()
            self.assertEqual(self.engine.store._eligible()[1], 0)
            # A full sync settles what it read like any other read.
            with self.reads() as calls, self.later():
                self.engine.sync()
            self.assertEqual(calls, [])

    def test_cli_sync_full(self):
        path = self.write()
        cli_state = self.root / 'cli-state'

        def run(*arguments):
            result = subprocess.run([sys.executable, str(CLI), '--vault', str(self.vault), '--state', str(cli_state), 'sync', *arguments],
                                    capture_output=True, text=True, encoding='utf-8', env=inherited_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        self.assertEqual(run()['indexed'], 1)
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        self.assertEqual(run('--full')['status'], 'succeeded')
        with subject.SyncEngine(self.vault, cli_state).store._connect() as db:
            payload, = db.execute("SELECT payload FROM records WHERE id='note'").fetchone()
        self.assertEqual(json.loads(payload)['text'], 'Omega calibration.\n')


def _load(name):
    spec = importlib.util.spec_from_file_location('v3_sync_stat_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SafeReplaceTest(unittest.TestCase):
    modules = ('beyin_v3_sync', 'beyin_v3_hook', 'beyin_v3_update')

    def test_posix_permission_error_is_raised_at_once(self):
        for name in self.modules:
            with self.subTest(module=name):
                module = subject if name == 'beyin_v3_sync' else _load(name)
                calls = []

                def refuse(source, destination):
                    calls.append(destination)
                    raise PermissionError(errno.EACCES, 'Permission denied', str(destination))

                with patch.object(module.os, 'name', 'posix'), patch.object(module.os, 'replace', refuse), \
                        patch.object(module.time, 'sleep') as sleep:
                    with self.assertRaises(PermissionError):
                        module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 1)
                sleep.assert_not_called()

    def test_windows_sharing_violation_is_retried(self):
        for name in self.modules:
            with self.subTest(module=name):
                module = subject if name == 'beyin_v3_sync' else _load(name)
                calls = []

                def busy(source, destination):
                    calls.append(destination)
                    if len(calls) < 3:
                        error = PermissionError(errno.EACCES, 'The process cannot access the file', str(destination))
                        error.winerror = 32
                        raise error

                with patch.object(module.os, 'name', 'nt'), patch.object(module.os, 'replace', busy), \
                        patch.object(module.time, 'sleep') as sleep:
                    module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 3)
                self.assertEqual(sleep.call_count, 2)
                # Not a sharing violation (e.g. a read-only target emulated without winerror): no retry.
                calls.clear()

                def refuse(source, destination):
                    calls.append(destination)
                    raise PermissionError(errno.EACCES, 'Access is denied', str(destination))

                with patch.object(module.os, 'name', 'nt'), patch.object(module.os, 'replace', refuse), \
                        patch.object(module.time, 'sleep'):
                    with self.assertRaises(PermissionError):
                        module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 1)

    def test_windows_read_only_target_is_raised_at_once(self):
        # #230: Windows refuses to replace a read-only file with the same WinError 5 a scanner
        # causes. No wait lifts that flag, so the caller gets the error without 225 ms of retries.
        for name in self.modules:
            with self.subTest(module=name):
                module = subject if name == 'beyin_v3_sync' else _load(name)
                calls = []

                def denied(source, destination):
                    calls.append(destination)
                    error = PermissionError(errno.EACCES, 'Access is denied', str(destination))
                    error.winerror = 5
                    raise error

                with patch.object(module.os, 'name', 'nt'), patch.object(module.os, 'replace', denied), \
                        patch.object(module.time, 'sleep') as sleep:
                    with patch.object(module, '_read_only', return_value=True):
                        with self.assertRaises(PermissionError):
                            module._safe_replace('a', 'b')
                    self.assertEqual((len(calls), sleep.call_count), (1, 0))
                    # Not read-only and still denied: a scanner, an open handle, a target
                    # pending deletion. Worth the retries.
                    calls.clear()
                    with patch.object(module, '_read_only', return_value=False):
                        with self.assertRaises(PermissionError):
                            module._safe_replace('a', 'b')
                    self.assertEqual((len(calls), sleep.call_count), (5, 4))

    def test_read_only_is_the_file_attribute(self):
        with tempfile.TemporaryDirectory(prefix='beyin-safe-replace-') as directory:
            target = Path(directory) / 'locked.md'
            target.write_bytes(b'locked')
            for name in self.modules:
                with self.subTest(module=name):
                    module = subject if name == 'beyin_v3_sync' else _load(name)
                    self.assertFalse(module._read_only(target))
                    self.assertFalse(module._read_only(Path(directory) / 'missing.md'))
                    target.chmod(stat.S_IREAD)
                    try:
                        # POSIX has no such attribute, and never retries a replace anyway.
                        self.assertEqual(module._read_only(target), os.name == 'nt')
                    finally:
                        target.chmod(stat.S_IREAD | stat.S_IWRITE)

    @unittest.skipUnless(os.name == 'nt', 'only Windows refuses to replace a read-only file')
    def test_a_real_read_only_target_fails_without_waiting(self):
        with tempfile.TemporaryDirectory(prefix='beyin-safe-replace-') as directory:
            source, target = Path(directory) / 'new.md', Path(directory) / 'locked.md'
            source.write_bytes(b'new')
            target.write_bytes(b'locked')
            target.chmod(stat.S_IREAD)
            try:
                with patch.object(subject.time, 'sleep') as sleep:
                    with self.assertRaises(PermissionError):
                        subject._safe_replace(source, target)
                sleep.assert_not_called()
                self.assertEqual(target.read_bytes(), b'locked')
            finally:
                target.chmod(stat.S_IREAD | stat.S_IWRITE)


if __name__ == '__main__':
    unittest.main()
