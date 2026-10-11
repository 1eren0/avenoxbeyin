#!/usr/bin/env python3
"""Directory names the user keeps out of the index (state/index.json), synthetic local fixtures only.

Nothing is pruned by name unless the user lists it: "Vendor" or "External" is a vendored code
tree in one vault and a notes folder in another. Every active name, a file that is not applied
and an indexed tree that looks vendored are visible in sync and doctor.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
MODULE = SCRIPTS / 'beyin_v3_sync.py'


def load_module():
    if not MODULE.is_file():
        raise AssertionError('SyncEngine not implemented')
    spec = importlib.util.spec_from_file_location('beyin_v3_exclude_subject', MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    scripts = str(MODULE.parent)
    sys.path.insert(0, scripts)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
        sys.path.remove(scripts)
    return module


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    scripts = str(SCRIPTS)  # adjacent modules import each other by name
    sys.path.insert(0, scripts)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
        sys.path.remove(scripts)
    return module


index_settings = load_file('beyin_v3_index_subject', SCRIPTS / 'beyin_v3_index.py')


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-exclude-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'Synthetic Beyin Çalışma'
        self.vault.mkdir()
        self.state = self.root / 'runtime'
        self.state.mkdir()
        self.engine = self.module.SyncEngine(self.vault, self.state)
        self.addCleanup(lambda: getattr(self.engine.store, 'close', lambda: None)())

    def note(self, relative, body, metadata=True):
        front = {'id': 'synthetic-' + relative.replace('/', '-').replace('.', '-'),
                 'kind': 'note', 'project': 'nebula', 'revision': 1,
                 'visibility': 'internal', 'facts': {'owner': 'Synthetic Reviewer'}}
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = ('---\n' + json.dumps(front, ensure_ascii=False) + '\n---\n' if metadata else '') + body
        path.write_bytes(text.encode('utf-8'))
        return path

    def config(self, value):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        (self.state / 'index.json').write_bytes(data)

    def sources(self):
        with self.engine.store._connect() as db:
            return sorted(row[0] for row in db.execute('SELECT source FROM markdown_sources'))


class DefaultWalkTest(EngineFixture):
    def test_vendored_looking_names_stay_indexed_without_a_list(self):
        # A business vault keeps supplier notes under Vendor/ and meeting notes under External/.
        expected = [self.note(relative, 'Nebula calibration note.\n').relative_to(self.vault).as_posix()
                    for relative in ('Vendor/Acme Tedarik.md', 'External/Toplanti.md', 'isler/vendored/karar.md',
                                     'Third_Party/Denetim.md', 'proje/3rdparty/SDL2/README.md', 'notlar/gercek.md')]
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.sources(), sorted(expected))
        self.assertNotIn('index_exclusions', result, 'a vault that never set the list keeps its exact sync output')

    def test_existing_excluded_dirs_still_apply(self):
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/node_modules/pkg/README.md', 'Nebula calibration node module.\n')
        self.note('proje/__pycache__/x.md', 'Nebula calibration cache.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.sources(), ['notlar/gercek.md'])


class UserListTest(EngineFixture):
    def test_listed_name_is_pruned_at_any_depth_and_case_as_a_whole_segment(self):
        self.config({'excluded_dirs': ['3rdParty', 'target']})
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3RDPARTY/lib/SECURITY.md', 'Nebula calibration vendored security.\n')
        self.note('proje/a/b/c/3rdparty/CONTRIBUTING.md', 'Nebula calibration vendored deep.\n')
        self.note('rust/target/debug/notes.md', 'Nebula calibration cargo tree.\n')
        # Only a whole path segment matches: a longer name or a file is not the listed directory.
        self.note('proje/3rdparty-notlari/karar.md', 'Nebula calibration own decision.\n')
        self.note('proje/my3rdparty/karar.md', 'Nebula calibration own decision two.\n')
        self.note('notlar/target.md', 'Nebula calibration note named like a directory.\n')
        # A vendored-looking name the user did not list stays indexed.
        self.note('Vendor/Acme Tedarik.md', 'Nebula calibration supplier.\n')
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded', 'the user chose these names: reported, not degraded')
        self.assertEqual(self.sources(), ['Vendor/Acme Tedarik.md', 'notlar/gercek.md', 'notlar/target.md',
                                          'proje/3rdparty-notlari/karar.md', 'proje/my3rdparty/karar.md'])
        self.assertEqual(result['index_exclusions'], {
            'excluded_dirs': ['3rdParty', 'target'], 'pruned': 3,
            'pruned_dirs': ['proje/3RDPARTY', 'proje/a/b/c/3rdparty', 'rust/target']})

    def test_unicode_form_and_turkish_case_match_on_both_sides(self):
        nfd = unicodedata.normalize('NFD', 'Önbellek Çıktısı')
        self.assertNotEqual(nfd, unicodedata.normalize('NFC', nfd))
        self.note(nfd + '/a.md', 'Nebula calibration decomposed folder.\n')
        self.note('Işık/b.md', 'Nebula calibration dotless capital.\n')
        self.note('İşler/c.md', 'Nebula calibration dotted capital.\n')
        self.note('Çıktı/d.md', 'Nebula calibration composed folder.\n')
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        # Typed in another case and, for the last one, decomposed.
        self.config({'excluded_dirs': ['önbellek çıktısı', 'ışık', 'işler', unicodedata.normalize('NFD', 'ÇIKTI')]})
        result = self.engine.sync()
        self.assertEqual(self.sources(), ['notlar/gercek.md'])
        self.assertEqual(result['index_exclusions']['pruned'], 4)
        key = index_settings.dir_key
        self.assertEqual({key('Işık'), key('IŞIK'), key('ışık')}, {key('ışık')})
        self.assertEqual({key('İşler'), key('İŞLER'), key('işler'), key('i\u0307şler')}, {key('işler')})
        self.assertNotEqual(key('Şifre'), key('Sifre'), 'only the two i letters fold; no other letter is merged')

    def test_excluded_tree_is_never_listed(self):
        self.config({'excluded_dirs': ['3rdparty']})
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3rdparty/SDL2/docs/deep/README.md', 'Nebula calibration vendored readme.\n')
        with mock.patch('os.scandir', wraps=os.scandir) as scandir:
            self.engine.sync()
        listed = [os.fspath(call.args[0]) for call in scandir.call_args_list]
        self.assertIn(str(self.vault / 'proje'), listed, 'the recorder sees the walk')
        self.assertEqual([path for path in listed if '3rdparty' in path], [],
                         'pruned from the walk, not filtered after it')

    def test_adding_a_name_removes_its_records_and_removing_it_restores_them(self):
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3rdparty/SDL2/README.md', 'Zirkonvendored upstream shader readme.\n')
        self.assertEqual(self.engine.sync()['indexed'], 2)
        found = self.engine.store.context_for('claude', 'zirkonvendored upstream shader', strict=True)['records']
        self.assertEqual([record['source'] for record in found], ['proje/3rdparty/SDL2/README.md'])
        self.config({'excluded_dirs': ['3rdparty']})
        result = self.engine.sync()
        self.assertEqual((result['indexed'], result['deleted']), (1, 1))
        self.assertEqual(self.sources(), ['notlar/gercek.md'])
        with self.engine.store._connect() as db:
            last = db.execute('SELECT event_type, record_id FROM events ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(last), ('delete', 'synthetic-proje-3rdparty-SDL2-README-md'))
        for strict in (True, False):
            found = self.engine.store.context_for('claude', 'zirkonvendored upstream shader', strict=strict)['records']
            self.assertEqual(found, [], 'gone from the passage path and the note-level path')
        self.config({'excluded_dirs': []})
        result = self.engine.sync()
        self.assertEqual((result['indexed'], result['deleted']), (2, 0))
        self.assertEqual(result['index_exclusions'], {'excluded_dirs': [], 'pruned': 0, 'pruned_dirs': []})
        found = self.engine.store.context_for('claude', 'zirkonvendored upstream shader', strict=True)['records']
        self.assertEqual([record['source'] for record in found], ['proje/3rdparty/SDL2/README.md'])
        (self.state / 'index.json').unlink()
        self.assertNotIn('index_exclusions', self.engine.sync())

    def test_a_new_source_under_a_listed_name_is_refused_before_it_is_written(self):
        # Without the refusal the file is written, never indexed, and the read-back reports a
        # projection conflict that does not name the cause.
        self.config({'excluded_dirs': ['Arşiv']})
        self.note('notes/kaynak.md', 'Nebula calibration real note.\n')
        self.engine.sync()
        with self.assertRaisesRegex(ValueError, 'excluded from the index'):
            self.engine.note_create('knowledge/arşiv/yeni.md', 'Nebula calibration created note.')
        with self.assertRaisesRegex(ValueError, 'excluded from the index'):
            self.engine.task_create('tasks/ARŞİV/gorev.md', 'Nebula calibration task.',
                                    {'id': 'synthetic-task', 'status': 'active', 'owner': 'Synthetic Reviewer'})
        self.assertFalse((self.vault / 'knowledge').exists())
        self.assertFalse((self.vault / 'tasks').exists())
        self.assertEqual(self.engine.note_create('knowledge/yeni.md', 'Nebula calibration created note.')['status'], 'succeeded')

    def test_built_in_and_hidden_names_are_harmless_and_receipts_keep_working(self):
        self.config({'excluded_dirs': ['node_modules', 'receipts', '.venv', 'target']})
        self.note('notes/kaynak.md', 'Nebula calibration receipt source.\n')
        self.note('proje/.venv/lib/README.md', 'Nebula calibration venv.\n')
        self.note('proje/node_modules/pkg/README.md', 'Nebula calibration node module.\n')
        result = self.engine.sync()
        self.assertNotIn('error', result['index_exclusions'])
        self.assertEqual(result['index_exclusions']['pruned'], 0, 'the walk skipped these before the list was asked')
        receipt = self.engine.receipt('synthetic-event-1', 'Nebula calibration receipt.', ['notes/kaynak.md'], 'codex')
        self.assertEqual(receipt['status'], 'succeeded')
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
        self.assertEqual(self.sources(), ['notes/kaynak.md'])


class RefusedConfigTest(EngineFixture):
    """A file that cannot be applied never breaks the sync: everything is indexed and the reason is shown."""

    def setUp(self):
        super().setUp()
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3rdparty/SDL2/README.md', 'Nebula calibration vendored readme.\n')
        self.everything = ['notlar/gercek.md', 'proje/3rdparty/SDL2/README.md']

    def refused(self, reason):
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded', 'a refused file is reported, not a degraded scan')
        self.assertEqual(self.sources(), self.everything)
        report = result['index_exclusions']
        self.assertEqual((report['excluded_dirs'], report['pruned'], report['pruned_dirs']), ([], 0, []))
        self.assertIn(reason, report['error'])
        return report

    def test_malformed_json(self):
        self.config(b'{not json')
        self.refused('not valid JSON')
        self.config(b'\xff\xfe\x00broken')
        self.refused('not valid JSON')

    def test_one_bad_entry_refuses_the_whole_list(self):
        # 3rdparty is a valid entry and a real directory: a half-applied list would prune it.
        for bad in ('../gizli', 'a/b', 'a\\b', '', ' 3rdparty ', '.', '..', 7, None, 'x' * 256, 'satir\nsonu'):
            with self.subTest(entry=bad):
                self.config({'excluded_dirs': ['3rdparty', bad]})
                self.refused('excluded_dirs')

    def test_wrong_shape_and_unknown_keys(self):
        for value in (['3rdparty'], {'excluded_dirs': '3rdparty'}, {'exclude_dirs': ['3rdparty']},
                      {'excluded_dirs': ['3rdparty'], 'schema': 2}):
            with self.subTest(value=value):
                self.config(value)
                self.refused('excluded_dirs')

    def test_entry_count_is_capped(self):
        self.config({'excluded_dirs': ['3rdparty'] + ['d%d' % i for i in range(64)]})
        self.refused('at most 64')
        self.config({'excluded_dirs': ['3rdparty'] + ['d%d' % i for i in range(63)]})
        self.assertEqual(self.engine.sync()['index_exclusions']['pruned_dirs'], ['proje/3rdparty'])

    def test_oversized_file_and_directory(self):
        self.config(json.dumps({'excluded_dirs': ['3rdparty']}).encode() + b' ' * 70000)
        self.refused('larger than')
        (self.state / 'index.json').unlink()
        (self.state / 'index.json').mkdir()
        self.refused('regular file')

    def test_symlinked_config(self):
        outside = self.root / 'outside.json'
        outside.write_bytes(json.dumps({'excluded_dirs': ['3rdparty']}).encode())
        try:
            (self.state / 'index.json').symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available here')
        self.refused('regular file')

    def test_bom_written_by_windows_editors_is_read(self):
        self.config(b'\xef\xbb\xbf' + json.dumps({'excluded_dirs': ['3rdparty']}).encode())
        result = self.engine.sync()
        self.assertNotIn('error', result['index_exclusions'])
        self.assertEqual(self.sources(), ['notlar/gercek.md'])

    def test_folders_read_back_through_the_index_cannot_be_excluded(self):
        self.note('🔮 850-Companion/Core.md', 'Nebula calibration companion.\n', metadata=False)
        self.everything = sorted(self.everything + ['🔮 850-Companion/Core.md'])
        for name in ('Knowledge', 'TASKS', '🔮 850-Companion', 'notes'):
            with self.subTest(name=name):
                self.config({'excluded_dirs': ['3rdparty', name]})
                self.refused('read back through the index')
        # With notes still listed, the refused file leaves note-create and its read-back intact.
        created = self.engine.note_create('notes/yeni.md', 'Nebula calibration created note.')
        self.assertEqual(created['status'], 'succeeded')

    def test_a_personalized_companion_folder_is_protected_too(self):
        self.note('Hafıza Echo/Core.md', 'Nebula calibration companion.\n', metadata=False)
        self.everything = sorted(self.everything + ['Hafıza Echo/Core.md'])
        self.config({'excluded_dirs': ['hafıza echo']})
        self.refused('read back through the index')


class SettingsFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-index-settings-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()

    def test_save_adds_removes_and_keeps_the_first_spelling(self):
        self.assertEqual(index_settings.read(self.state, self.vault), ([], None, False))
        self.assertEqual(index_settings.save(self.state, self.vault, ['3rdparty', 'Target']), ['3rdparty', 'Target'])
        self.assertEqual(index_settings.save(self.state, self.vault, ['TARGET', unicodedata.normalize('NFD', 'Önbellek')]),
                         ['3rdparty', 'Target', 'Önbellek'])
        self.assertEqual(json.loads((self.state / 'index.json').read_bytes().decode('utf-8')),
                         {'schema': 1, 'excluded_dirs': ['3rdparty', 'Target', 'Önbellek']})
        self.assertEqual(index_settings.save(self.state, self.vault, remove=['3RDPARTY', 'absent']), ['Target', 'Önbellek'])
        self.assertEqual(index_settings.read(self.state, self.vault), (['Target', 'Önbellek'], None, True))

    def test_a_refused_change_writes_nothing(self):
        index_settings.save(self.state, self.vault, ['3rdparty'])
        before = (self.state / 'index.json').read_bytes()
        for name in ('notes', 'knowledge', 'tasks', '🔮 850-Companion', 'a/b', '..', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                index_settings.save(self.state, self.vault, [name])
        with self.assertRaises(ValueError):
            index_settings.save(self.state, self.vault, ['d%d' % i for i in range(64)])
        self.assertEqual((self.state / 'index.json').read_bytes(), before)
        self.assertEqual([entry.name for entry in self.state.iterdir()], ['index.json'], 'no temporary file is left')

    def test_an_invalid_file_is_never_rewritten(self):
        self.state.mkdir()
        (self.state / 'index.json').write_bytes(b'{not json')
        with self.assertRaisesRegex(ValueError, 'fix or remove it first'):
            index_settings.save(self.state, self.vault, ['3rdparty'])
        self.assertEqual((self.state / 'index.json').read_bytes(), b'{not json')


class VendoredHintTest(unittest.TestCase):
    def test_only_trees_with_upstream_documents_are_pointed_at(self):
        sources = ['Vendor/Acme Tedarik.md', 'Vendor/Sozlesmeler/2026.md', 'External/Toplanti.md',  # notes folders
                   'proje/3rdparty/SDL2/README.md', 'proje/3rdparty/SDL2/SECURITY.md', 'proje/3rdparty/SDL2/docs/x.md',
                   'go/Vendor/pkg/CHANGELOG.md', 'notlar/vendor.md', 'notlar/gercek.md']
        self.assertEqual(index_settings.vendored_hint(sources), {
            'notes': 4, 'dirs': [{'dir': 'proje/3rdparty', 'notes': 3}, {'dir': 'go/Vendor', 'notes': 1}],
            'names': ['3rdparty', 'Vendor'], 'truncated': False})
        self.assertIsNone(index_settings.vendored_hint(sources[:3] + sources[-2:]))

    def test_doctor_leaves_out_what_the_list_already_covers(self):
        with tempfile.TemporaryDirectory(prefix='beyin-index-hint-') as tmp:
            state = Path(tmp)
            sources = ['proje/3rdparty/SDL2/README.md', 'go/vendor/pkg/README.md']
            self.assertEqual(index_settings.doctor(state, None, sources)['vendored_hint']['names'], ['3rdparty', 'vendor'])
            (state / 'index.json').write_bytes(b'{"excluded_dirs": ["3RDPARTY"]}')
            report = index_settings.doctor(state, None, sources)
            self.assertEqual((report['status'], report['excluded_dirs']), ('active', ['3RDPARTY']))
            self.assertEqual(report['vendored_hint']['names'], ['vendor'])
            (state / 'index.json').write_bytes(b'{"excluded_dirs": ["3rdparty", "vendor"]}')
            self.assertNotIn('vendored_hint', index_settings.doctor(state, None, sources))
            (state / 'index.json').write_bytes(b'[]')
            report = index_settings.doctor(state, None, sources)
            self.assertEqual((report['status'], report['excluded_dirs']), ('invalid', []))
            self.assertIn('error', report)


if __name__ == '__main__':
    unittest.main()
