#!/usr/bin/env python3
"""#304: single-keyword strict retrieval must work without weakening multiword gates."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
import beyin_v3  # noqa: E402


class SingleTermStrictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-single-term-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        self.vault.mkdir()
        self.store = beyin_v3.MemoryStore(root / "state", self.vault)
        self.addCleanup(self.store.close)

    def ingest(self, ident, text, source, **kwargs):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.store.ingest(dict(id=ident, kind="note", status="active", visibility="internal",
                               text=text, facts={}, source=source,
                               updated_at="2026-10-11T10:00:00Z", **kwargs))

    def ids(self, query, passages=False, **kwargs):
        if passages:
            result = self.store.context_for("claude", query, strict=True, **kwargs)
        else:
            result = self.store.retrieve(query, strict=True, **kwargs)
        return [record["id"] for record in result["records"]]

    def test_single_keyword_reaches_both_strict_paths(self):
        self.ingest("math", "Calculus derivatives are explained here.", "knowledge/calculus.md")
        self.ingest("other", "A grocery list for this week.", "notes/groceries.md")
        self.assertEqual(self.ids("calculus"), ["math"])
        self.assertEqual(self.ids("calculus", passages=True), ["math"])

    def test_multiword_queries_still_require_two_shared_terms(self):
        self.ingest("math", "Calculus derivatives are explained here.", "knowledge/calculus.md")
        for passage_mode in (False, True):
            with self.subTest(passages=passage_mode):
                self.assertEqual(self.ids("calculus quantum", passages=passage_mode), [])
                self.assertEqual(self.ids("unknownword", passages=passage_mode), [])
                self.assertEqual(self.ids("", passages=passage_mode), [])

    def test_single_keyword_still_respects_visibility_and_source_gates(self):
        self.ingest("math", "Calculus derivatives are explained here.", "knowledge/calculus.md")
        self.ingest("private", "Calculus confidential details.", "notes/hidden.md",
                    visibility="private")
        self.ingest("daily", "Calculus was mentioned today.", "daily/2026-10-11.md")
        self.ingest("stale", "Calculus old reference.", "knowledge/old.md")
        (self.vault / "knowledge/old.md").write_text("Changed after indexing.", encoding="utf-8")
        for passage_mode in (False, True):
            with self.subTest(passages=passage_mode):
                self.assertEqual(self.ids("calculus", passages=passage_mode), ["math"])

    def test_incidental_keyword_in_long_hub_stays_below_note_level_floor(self):
        hub = "Weekly hub: calculus. " + " ".join("topic" + str(i) for i in range(400))
        self.ingest("hub", hub, "notes/hub.md")
        self.assertEqual(self.ids("calculus"), [])


if __name__ == "__main__":
    unittest.main()
