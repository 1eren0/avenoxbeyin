#!/usr/bin/env python3
"""#304: bounded hook acknowledgements, durable dedupe and doctor migration."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
import beyin_v3 as runtime  # noqa: E402
import beyin_v3_hook as hook  # noqa: E402
import beyin_v3_hook_done as ledger  # noqa: E402


class HookDoneLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v3-hook-done-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "state"
        self.store = runtime.MemoryStore(self.state, self.vault)
        self.done = self.state / "hook-done"
        self.done.mkdir()
        self.addCleanup(self.store.close)

    def marker(self, name, harness="claude", event="SessionEnd", old=True):
        key = hashlib.sha256(name.encode()).hexdigest()
        path = self.done / (key + ".json")
        path.write_text(json.dumps({"harness": harness, "event": event,
                                    "session": "private-session", "at": 123}), encoding="utf-8")
        if old:
            stamp = time.time() - ledger.RETENTION_SECONDS - 60
            os.utime(path, (stamp, stamp))
        return key, path

    def test_lazy_migration_and_deduplication(self):
        key, old = self.marker("legacy", event="SessionStart")
        with sqlite3.connect(self.store.database) as db:
            self.assertFalse(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_done_ledger'"
            ).fetchone())
        self.assertFalse(ledger.acknowledged(self.state, key))
        self.assertEqual(ledger.summary(self.state, ("claude",))["acknowledged_events"], 1)
        self.assertEqual(ledger.prune(self.store), 1)
        self.assertFalse(old.exists())
        self.assertTrue(ledger.acknowledged(self.state, key))
        report = ledger.summary(self.state, ("claude", "codex"))
        self.assertEqual(report["acknowledged_events"], 1)
        self.assertEqual(report["lifecycle"]["claude"], {
            "status": "observed_metadata", "events": ["SessionStart"]})
        self.assertEqual(report["lifecycle"]["codex"]["status"], "never_seen")
        with sqlite3.connect(self.store.database) as db:
            row = db.execute("SELECT key,harness,event FROM hook_done_ledger").fetchone()
        self.assertEqual(row, (key, "claude", "SessionStart"))
        self.assertNotIn("private-session", str(row))

    def test_recent_files_remain_and_empty_scan_is_throttled(self):
        key, recent = self.marker("recent", old=False)
        self.assertEqual(ledger.prune(self.store), 0)
        self.assertTrue(recent.exists())
        with patch.object(Path, "glob", side_effect=AssertionError("unexpected directory scan")):
            self.assertEqual(ledger.prune(self.store), 0)
        self.assertFalse(ledger.acknowledged(self.state, key))

    def test_batch_limit_drains_backlog_without_forgetting(self):
        items = [self.marker(f"older-{i}") for i in range(ledger.BATCH_SIZE + 1)]
        self.assertEqual(ledger.prune(self.store), ledger.BATCH_SIZE)
        self.assertEqual(len(list(self.done.glob("*.json"))), 1)
        self.assertEqual(ledger.prune(self.store), 1)
        self.assertEqual(list(self.done.glob("*.json")), [])
        self.assertEqual(ledger.summary(self.state, ("claude",))["acknowledged_events"], len(items))
        self.assertTrue(all(ledger.acknowledged(self.state, key) for key, _path in items))

    def test_sql_error_cannot_delete_unarchived_marker(self):
        key, old = self.marker("error")
        with patch.object(ledger, "_CREATE", "INVALID SQL"):
            with self.assertRaises(sqlite3.OperationalError):
                ledger.prune(self.store)
        self.assertTrue(old.exists())
        self.assertFalse(ledger.acknowledged(self.state, key))
        self.assertEqual(ledger.prune(self.store), 1)
        self.assertTrue(ledger.acknowledged(self.state, key))

    def test_unlink_failure_after_commit_is_retryable_and_not_double_counted(self):
        key, old = self.marker("interrupted")
        original = Path.unlink
        def deny_done_unlink(path, *args, **kwargs):
            if path == old:
                raise PermissionError("simulated interruption after DB commit")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", deny_done_unlink):
            with self.assertRaises(PermissionError):
                ledger.prune(self.store)
        self.assertTrue(old.exists())
        self.assertTrue(ledger.acknowledged(self.state, key))
        report = ledger.summary(self.state, ("claude",))
        self.assertEqual(report["acknowledged_events"], 1)
        self.assertEqual(report["lifecycle"]["claude"]["events"], ["SessionEnd"])
        self.assertEqual(ledger.prune(self.store), 1)
        self.assertFalse(old.exists())

    def test_invalid_or_symlinked_file_is_never_deleted(self):
        key, invalid = self.marker("invalid")
        invalid.write_text("{bad-json", encoding="utf-8")
        external = self.root / "outside.json"
        external.write_text('{"harness":"claude","event":"Stop"}', encoding="utf-8")
        link = self.done / (hashlib.sha256(b"link").hexdigest() + ".json")
        try:
            link.symlink_to(external)
        except (OSError, NotImplementedError):
            link = None
        self.assertEqual(ledger.prune(self.store), 0)
        self.assertTrue(invalid.exists())
        self.assertFalse(ledger.acknowledged(self.state, key))
        self.assertTrue(external.exists())
        if link is not None:
            self.assertTrue(link.is_symlink())

    def test_queue_ignores_archived_event_key(self):
        identity = "durable-ack-event"
        key, old = self.marker(identity)
        self.assertEqual(ledger.prune(self.store), 1)
        self.assertFalse(old.exists())
        payload = {"event_id": identity, "hook_event_name": "SessionEnd",
                   "session_id": "synthetic", "prompt": ""}
        self.assertEqual(hook.enqueue_event(self.vault, self.state, payload, "claude"), key)
        self.assertEqual(list((self.state / "hook-queue").glob("*.json")), [])

    def test_actual_queue_drain_and_late_replay(self):
        from beyin_v3_sync import SyncEngine
        note = self.vault / "notes/task.md"
        note.parent.mkdir()
        note.write_text('---\n' + json.dumps({
            "id": "task", "kind": "task", "revision": 1,
            "status": "active", "visibility": "internal"
        }) + '\n---\nSynthetic task body.\n', encoding="utf-8")
        engine = SyncEngine(self.vault, self.state)
        engine.sync()
        payload = {"event_id": "e2e-replay", "session_id": "synthetic",
                   "hook_event_name": "SessionEnd"}
        key = hook.enqueue_event(self.vault, self.state, payload, "claude")
        self.assertEqual(len(list((self.state / "hook-queue").glob("*.json"))), 1)
        result = hook.drain_queue(self.vault, self.state)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["failed"], 0)
        done_file = self.done / (key + ".json")
        self.assertTrue(done_file.exists())
        # The successful drain performed a fresh retention scan. Simulate the
        # next week rather than bypassing the daily scan marker.
        future = time.time() + ledger.RETENTION_SECONDS + 60
        self.assertEqual(ledger.prune(self.store, now=future), 1)
        self.assertFalse(done_file.exists())
        self.assertEqual(hook.enqueue_event(self.vault, self.state, payload, "claude"), key)
        self.assertEqual(list((self.state / "hook-queue").glob("*.json")), [])

    def test_doctor_cli_preserves_archived_lifecycle(self):
        self.marker("old-claude", harness="claude", event="SessionStart")
        self.marker("recent-codex", harness="codex", event="Stop", old=False)
        self.assertEqual(ledger.prune(self.store), 1)
        process = subprocess.run(
            [sys.executable, str(ROOT / "scripts/beyin_v3.py"), "--vault", str(self.vault),
             "--state", str(self.state), "doctor"],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["acknowledged_events"], 2)
        self.assertEqual(report["lifecycle"]["claude"]["events"], ["SessionStart"])
        self.assertEqual(report["lifecycle"]["codex"]["events"], ["Stop"])

    def test_doctor_reports_legacy_recent_and_archived_history(self):
        _old_key, _old = self.marker("archive", harness="claude", event="SessionStart")
        _live_key, live = self.marker("recent", harness="codex", event="Stop", old=False)
        self.assertEqual(ledger.prune(self.store), 1)
        self.assertTrue(live.exists())
        report = ledger.summary(self.state, ("claude", "codex"))
        self.assertEqual(report["acknowledged_events"], 2)
        self.assertEqual(report["lifecycle"]["claude"]["events"], ["SessionStart"])
        self.assertEqual(report["lifecycle"]["codex"]["events"], ["Stop"])


if __name__ == "__main__":
    unittest.main()
