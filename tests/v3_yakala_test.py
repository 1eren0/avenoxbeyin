"""Optional capture tool (yakala): exercised through a real install, the installed entry and hook.

Offline: `isle` runs with every helper tool hidden, so nothing reaches the network and the
popup/hotkey paths are never started.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env
from v3_package_helpers import clean_environ, isolated_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
INBOX = '📥 000-Inbox/Yakala'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402

# Stands in for `npx defuddle`, `yt-dlp` and `whisper` as a real child process. It answers from
# scenario.json next to it, logs every command line to calls.jsonl and never opens a socket. A subtitle
# request behaves like yt-dlp: --sub-langs is a comma list of regexes, a manual track wins over an
# automatic one of the same name, and each matching track becomes sub.<track>.vtt.
FAKE_TOOL = r'''
import json, re, sys
from pathlib import Path

here = Path(__file__).resolve().parent
name, args = sys.argv[1], sys.argv[2:]
scenario = json.loads((here / 'scenario.json').read_text(encoding='utf-8'))
with open(here / 'calls.jsonl', 'a', encoding='utf-8') as log:
    log.write(json.dumps([name] + args) + '\n')


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


def too_many():
    sys.stderr.write('ERROR: Unable to download video subtitles: HTTP Error 429: Too Many Requests\n')
    raise SystemExit(1)


if name == 'npx':
    answers = scenario.get('defuddle') or {}
    answer = answers.get(value('--lang') or '', answers.get(''))
    if not answer:
        raise SystemExit(1)
    sys.stdout.write(json.dumps(answer))
elif name == 'whisper':
    Path(value('--output_dir'), 'ses.txt').write_text(scenario['audio'], encoding='utf-8')
elif '-J' in args:
    if not scenario.get('video'):
        too_many()
    sys.stdout.write(json.dumps(scenario['video']))
elif '--sub-langs' in args:
    if '--load-info-json' in args:
        json.loads(Path(value('--load-info-json')).read_text(encoding='utf-8'))
    video, offered = scenario.get('video') or {}, {}
    if '--write-subs' in args:
        offered.update({track: 'manual' for track in video.get('subtitles') or {}})
    if '--write-auto-subs' in args:
        for track in video.get('automatic_captions') or {}:
            offered.setdefault(track, 'auto')
    earlier = sum('--sub-langs' in json.loads(line) for line in (here / 'calls.jsonl').read_text(encoding='utf-8').splitlines()) - 1
    if earlier < scenario.get('fail_first', 0):
        too_many()
    for track, kind in offered.items():
        if any(re.fullmatch(pattern, track) for pattern in value('--sub-langs').split(',')):
            words = scenario.get('speech', {}).get(kind + ':' + track) or (kind + ' track ' + track + ' ') * 8
            Path(value('-o').replace('%(ext)s', track + '.vtt')).write_text(
                'WEBVTT\nKind: captions\n\n00:00:01.000 --> 00:00:09.000\n' + words + '\n', encoding='utf-8')
elif '-f' in args and scenario.get('audio'):
    Path(value('-o').replace('%(ext)s', 'webm')).write_bytes(b'ses')
else:
    raise SystemExit(1)
'''

VIDEO = 'https://www.youtube.com/watch?v=Wqq0mwYmhcQ'
DUBS = ('ar', 'bn', 'de-DE', 'es-US', 'fr-FR', 'hi', 'id', 'it', 'iw', 'ja', 'ko', 'ml', 'nl-NL', 'pa', 'pl', 'pt-BR',
        'ru', 'ta', 'te', 'uk')
ENGLISH = 'So, I gave the same prompt to both models across four different builds and wrote down what every run cost.'
ARABIC = 'لذا، قمتُ بتطبيق نفس التعليمات على النموذجين في أربع تجارب مختلفة، ثم سجّلتُ تكلفة كل تجربة منها بالتفصيل.'
TURKISH = 'Aynı istemi iki modele de verdim; ölçtüğüm maliyeti şöyle yazdım: İstanbul, Iğdır, Çorum, Şırnak, Ödemiş, Ünye.'


def caption(source, target=None):
    """One automatic caption entry as yt-dlp lists it; `tlang` marks a machine translation of `source`."""
    url = 'https://www.youtube.com/api/timedtext?v=Wqq0mwYmhcQ&kind=asr&lang=' + source + ('&tlang=' + target if target else '')
    return [{'ext': ext, 'url': url + '&fmt=' + ext} for ext in ('json3', 'vtt')]


def video(language=None, audio=(), manual=(), recognised=(), translated=()):
    """The part of a `yt-dlp -J` answer the capture reads. `audio` is (language, language_preference) pairs."""
    automatic = {track: caption(track.removesuffix('-orig')) for track in recognised}
    for track in translated:  # the untranslated entry, if any, comes first; yt-dlp takes the last one
        automatic[track] = automatic.get(track, []) + caption('uk', track)
    return {'id': 'Wqq0mwYmhcQ', 'title': 'I Tested Two Models', 'language': language,
            'formats': [{'format_id': '251-' + str(index), 'acodec': 'opus', 'language': code, 'language_preference': mark}
                        for index, (code, mark) in enumerate(audio)],
            'subtitles': {track: [{'ext': 'vtt', 'url': 'https://www.youtube.com/api/timedtext?lang=' + track}] for track in manual},
            'automatic_captions': automatic}


def dubbed():
    """Like the video in #277: spoken English, 20 automatic dubs, one `-orig` track per audio track."""
    return video('en-US', audio=[('en-US', 10)] + [(code, -1) for code in DUBS],
                 recognised=['en', 'en-orig'] + [code + '-orig' for code in DUBS],
                 translated=['en', 'tr'] + [code for code in DUBS])


def defuddle(language, words=None):
    """A `defuddle parse --json` answer; without words the video has no transcript, only its description."""
    body = '## Transcript\n\n### Intro\n\n**0:00** · ' + words if words else 'Video description without a transcript. ' * 4
    return {'title': 'I Tested Two Models', 'author': 'Kanal', 'published': '2026-09-16T05:37:48-07:00', 'site': 'YouTube',
            'language': language, 'content': '![](' + VIDEO + ')\n\n' + body}


class YakalaUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-unit-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'Örnek Beyin'
        self.vault.mkdir()

    def test_canonical_merges_url_forms(self):
        self.assertEqual(yakala.canonical('https://youtu.be/abc123?t=40'), yakala.canonical('https://www.youtube.com/watch?v=abc123&list=x'))
        self.assertEqual(yakala.canonical('https://twitter.com/a/status/42'), yakala.canonical('https://x.com/b/status/42?s=20'))
        self.assertNotEqual(yakala.canonical('https://a.com/x'), yakala.canonical('https://a.com/y'))

    def test_kind_and_slug(self):
        self.assertEqual(yakala.kind_of('https://m.youtube.com/watch?v=1'), 'youtube')
        self.assertEqual(yakala.kind_of('https://x.com/a/status/1'), 'x')
        self.assertEqual(yakala.kind_of('https://mail.google.com/mail/u/0/#inbox/1'), 'mail')
        self.assertEqual(yakala.kind_of(None, app='Mail'), 'mail')
        self.assertEqual(yakala.kind_of(None), 'metin')
        self.assertEqual(yakala.slug('Kısa tut, İyi düşün: ağ'), 'kisa-tut-iyi-dusun-ag')
        self.assertEqual(yakala.url_slug('https://www.youtube.com/watch?v=AbC'), 'youtube-abc')
        self.assertEqual(yakala.url_slug('https://x.com/karpathy/status/99'), 'x-karpathy-99')

    def test_capture_writes_one_card_and_dedupes(self):
        first = yakala.capture(self.vault, url='https://www.youtube.com/watch?v=abc', why='limit gözlemi')
        again = yakala.capture(self.vault, url='https://youtu.be/abc', why='ikinci bakış')
        self.assertEqual(first['status'], 'yakalandi')
        self.assertEqual(again['status'], 'mevcut_karta_eklendi')
        self.assertEqual(again['id'], first['id'])
        cards = yakala.cards(self.vault)
        self.assertEqual(len(cards), 1)
        meta, body = cards[0]['meta'], cards[0]['body']
        self.assertEqual((meta['durum'], meta['kaynak_turu'], meta['visibility']), ('bekliyor', 'youtube', 'internal'))
        self.assertIn('limit gözlemi', body)
        self.assertIn('ikinci bakış', body)
        self.assertEqual(yakala.pending(self.vault), 1)

    def test_capture_requires_something(self):
        with self.assertRaises(ValueError):
            yakala.capture(self.vault, why='yalniz neden')

    def test_private_kinds_and_files(self):
        source = Path(self.tmp.name) / 'not.txt'
        source.write_text('dosyadaki ders', encoding='utf-8')
        result = yakala.capture(self.vault, files=[source], why='belge')
        card = yakala.find_card(self.vault, result['id'])
        self.assertEqual(card['meta']['visibility'], 'private')
        self.assertTrue((self.vault / INBOX / 'dosyalar/not.txt').is_file())
        text = yakala.capture(self.vault, text='pano metni')
        self.assertEqual(yakala.find_card(self.vault, text['id'])['meta']['visibility'], 'private')

    def test_process_offline_and_finish(self):
        none = {name: None for name in ('npx', 'yt_dlp', 'whisper', 'pdftotext', 'ffmpeg')}
        note = yakala.capture(self.vault, text='Bağlam kaybı en pahalı kalem.', why='ders')
        clipped = self.vault / INBOX / 'clip.md'
        clipped.write_text('---\ntur: yakala\ndurum: bekliyor\nkaynak_turu: makale\nurl: "https://ornek.com/yazi"\n---\n\n'
                           '# Yazı\n\n## Neden\n\n\n\n## Icerik\n\n' + ('Uzun makale metni. ' * 30) + '\n', encoding='utf-8')
        video = yakala.capture(self.vault, url='https://www.youtube.com/watch?v=zzz')
        with patch.object(yakala, 'tools', return_value=none):
            result = yakala.process(self.vault)
        by_id = {entry['id']: entry for entry in result['kartlar']}
        self.assertEqual(by_id[note['id']]['yontem'], 'metin')
        self.assertEqual(by_id['clip']['yontem'], 'web-clipper')
        self.assertEqual(by_id[video['id']]['durum'], 'hata')
        raw = (self.vault / by_id['clip']['ham']).read_text(encoding='utf-8')
        self.assertIn('Uzun makale metni.', raw)
        self.assertIn('visibility: private', raw)
        # The clipped page body moves out of the indexed card into the dot folder.
        self.assertNotIn('Uzun makale metni.', (self.vault / INBOX / 'clip.md').read_text(encoding='utf-8'))
        self.assertEqual(yakala.pending(self.vault), 0)
        concept = self.vault / 'knowledge/concepts/baglam.md'
        concept.parent.mkdir(parents=True)
        concept.write_text('# Bağlam\n', encoding='utf-8')
        done = yakala.finish(self.vault, note['id'], ['knowledge/concepts/baglam.md'], 'tek ders')
        self.assertEqual(done['status'], 'islendi')
        card = yakala.find_card(self.vault, note['id'])
        self.assertEqual(card['meta']['durum'], 'islendi')
        self.assertIn('[[knowledge/concepts/baglam]]', card['body'])
        with self.assertRaises(ValueError):
            yakala.finish(self.vault, note['id'], ['knowledge/concepts/yok.md'])

    def test_vtt_text(self):
        raw = ('WEBVTT\nKind: captions\n\n00:00:00.000 --> 00:00:02.000\nmerhaba <c>dunya</c>\n\n'
               '00:00:02.000 --> 00:00:04.000\nmerhaba dunya\nikinci satir\n\n00:00:40.000 --> 00:00:42.000\nyeni paragraf\n'
               '\n01:00:05.000 --> 01:00:06.000\nsaat\n')
        text = yakala.vtt_text(raw)
        self.assertEqual(text.count('merhaba dunya'), 1)
        self.assertIn('**0:00** merhaba dunya ikinci satir', text)
        self.assertIn('**0:40** yeni paragraf', text)
        self.assertIn('**1:00:05** saat', text)

    def test_repair_mojibake(self):
        good = 'Agent loop’ta en pahalı şey bağlam kaybı'
        self.assertEqual(yakala.repair_mojibake(good.encode('utf-8').decode('mac_roman')), good)
        self.assertEqual(yakala.repair_mojibake('pahalÄ± ÅŸey'), 'pahalı şey')  # cp1252 variant
        for text in ('Müşteri Şifre Arşiv', 'café', 'plain'):
            self.assertEqual(yakala.repair_mojibake(text), text)

    def test_clipper_template_is_importable(self):
        template = yakala.clipper_template()
        for field in ('name', 'behavior', 'properties', 'noteContentFormat', 'noteNameFormat', 'path'):
            self.assertIn(field, template)
        self.assertEqual(template['path'], INBOX)
        names = {prop['name']: prop['value'] for prop in template['properties']}
        self.assertEqual((names['tur'], names['durum']), ('yakala', 'bekliyor'))
        self.assertTrue(all(prop['type'] in ('text', 'multitext', 'number', 'checkbox', 'date', 'datetime')
                            for prop in template['properties']))

    def test_hotkey_specs(self):
        self.assertEqual(yakala.parse_hotkey('ctrl+alt+b'), (['ctrl', 'alt'], 'b'))
        self.assertEqual(yakala.parse_hotkey('Option+Command+K'), (['alt', 'cmd'], 'K'))
        self.assertEqual(yakala.parse_hotkey('⌘⇧space'), (['shift', 'cmd'], 'space'))
        self.assertEqual(yakala.parse_hotkey('cmd++'), (['cmd'], '+'))
        self.assertEqual(yakala.parse_hotkey('cmd+"'), (['cmd'], '"'))
        self.assertEqual(yakala.parse_hotkey('f9'), ([], 'f9'))
        for bad in ('', 'b', 'hyper+b', 'cmd+', 'cmd+pageup'):
            with self.assertRaises(ValueError):
                yakala.parse_hotkey(bad)
        self.assertEqual(yakala.windows_hotkey('ctrl+alt+b'), ('CTRL+ALT+B', 'Ctrl+Alt+B'))
        self.assertEqual(yakala.windows_hotkey('alt+shift+f2')[0], 'ALT+SHIFT+F2')
        for bad in ('cmd+b', 'shift+b', 'ctrl+"'):
            with self.assertRaises(ValueError):
                yakala.windows_hotkey(bad)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS key codes')
    def test_mac_hotkey_codes(self):
        self.assertEqual(yakala.mac_hotkey('ctrl+alt+b')[:2], (11, 0x1800))
        self.assertEqual(yakala.mac_hotkey('cmd+shift+space'), (49, 0x300, '⇧⌘Space'))
        self.assertEqual(yakala.mac_hotkey('alt+f5')[:2], (96, 0x800))

    def test_vault_before_or_after_command(self):
        yakala.capture(self.vault, url='https://ornek.com/a')
        for argv in (['--vault', str(self.vault), '--json', 'liste'], ['liste', '--vault', str(self.vault), '--json']):
            with patch('sys.stdout', new_callable=__import__('io').StringIO) as out:
                yakala.main(argv)
            self.assertEqual(json.loads(out.getvalue())['bekleyen'], 1)

    def test_bad_hotkey_writes_nothing(self):
        state = Path(self.tmp.name) / 'state'
        # The spec is parsed before any platform call, so the macOS branch rejects it on every OS.
        with patch.object(yakala.sys, 'platform', 'darwin'):
            with self.assertRaises(ValueError):
                yakala.install(self.vault, state, spec='hyper+b')
        self.assertFalse((self.vault / INBOX).exists())
        self.assertFalse(state.exists())

    def test_uninstall_leaves_another_vaults_listener(self):
        import plistlib
        agent = Path(self.tmp.name) / 'agent.plist'
        agent.write_bytes(plistlib.dumps({'ProgramArguments': ['python3', 'x.py', 'dinle', '--vault', '/baska/vault']}))
        state = Path(self.tmp.name) / 'state'
        with patch.object(yakala, '_launch_agent', return_value=agent), patch.object(yakala.sys, 'platform', 'darwin'), \
                patch.object(yakala.subprocess, 'run') as run:
            result = yakala.uninstall(self.vault, state)
        run.assert_not_called()
        self.assertTrue(agent.exists())
        self.assertNotIn('LaunchAgent', result['kaldirilan'])

    def test_inbox_report_skips_processed_cards(self):
        import beyin_v3_hygiene as hygiene
        waiting = yakala.capture(self.vault, url='https://ornek.com/a')
        done = yakala.capture(self.vault, text='bitti')
        card = yakala.find_card(self.vault, done['id'])
        yakala.write_card(card['path'], dict(card['meta'], durum='islendi'), card['body'])
        rows = hygiene.inbox_report(self.vault)['folders']
        self.assertEqual([row['notes'] for row in rows if row['folder'] == '📥 000-Inbox'], [1])
        self.assertTrue(waiting['id'])

    def test_clipper_card_with_quoted_yaml_is_pending(self):
        folder = self.vault / INBOX
        folder.mkdir(parents=True)
        (folder / 'a.md').write_text('---\ntur: yakala\ndurum: "bekliyor"\nurl: "https://a.com"\n---\n\n# A\n', encoding='utf-8')
        (folder / 'b.md').write_text('---\ntur: not\ndurum: bekliyor\n---\n', encoding='utf-8')
        self.assertEqual(yakala.pending(self.vault), 1)


class YakalaVideoLanguageTest(unittest.TestCase):
    """#277: the transcript is in the language spoken in the video; the card names the language it stored
    and warns only on evidence of a wrong one (a known mismatch, or a dubbed video of unknown language)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-dil-')
        self.addCleanup(self.tmp.cleanup)
        self.vault, self.bin = Path(self.tmp.name) / 'Örnek Beyin', Path(self.tmp.name) / 'bin'
        self.vault.mkdir()
        self.bin.mkdir()
        (self.bin / 'fake_tool.py').write_text(FAKE_TOOL, encoding='utf-8')
        environ = clean_environ()
        environ.start()
        self.addCleanup(environ.stop)
        real_run = yakala._run

        def run(command, timeout, cwd=None):  # the real subprocess call, aimed at the stand-in
            self.assertGreater(timeout, 0)
            return real_run([sys.executable, str(self.bin / 'fake_tool.py'), Path(command[0]).name, *command[1:]], timeout, cwd)
        patcher = patch.object(yakala, '_run', run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def isle(self, scenario, npx=True, yt_dlp=True, whisper=False, url=VIDEO, **options):
        """Capture `url` once, process the queue against `scenario`; returns (result entry, card meta, raw text)."""
        (self.bin / 'scenario.json').write_text(json.dumps(scenario), encoding='utf-8')
        (self.bin / 'calls.jsonl').write_text('', encoding='utf-8')
        found = {'npx': 'npx' if npx else None, 'yt_dlp': 'yt-dlp' if yt_dlp else None,
                 'whisper': 'whisper' if whisper else None, 'pdftotext': None, 'ffmpeg': None}
        card = next((card['id'] for card in yakala.cards(self.vault) if card['meta'].get('url') == url), None) \
            or yakala.capture(self.vault, url=url)['id']
        with patch.object(yakala, 'tools', return_value=found):
            entry = yakala.process(self.vault, [card], **options)['kartlar'][0]
        raw = (self.vault / entry['ham']).read_text(encoding='utf-8') if entry.get('ham') else ''
        return entry, yakala.find_card(self.vault, card)['meta'], raw

    def calls(self, tool=None):
        lines = (self.bin / 'calls.jsonl').read_text(encoding='utf-8').splitlines()
        return [call for call in map(json.loads, lines) if tool in (None, call[0])]

    def track_requests(self):
        """What each yt-dlp subtitle request asked for: (--sub-langs value, automatic?)."""
        return [(call[call.index('--sub-langs') + 1], '--write-auto-subs' in call) for call in self.calls('yt-dlp')
                if '--sub-langs' in call]

    def test_dubbed_video_asks_defuddle_for_the_spoken_language(self):
        answers = {'': defuddle('ar', ARABIC), 'en-US': defuddle('en', ENGLISH)}
        entry, meta, raw = self.isle({'video': dubbed(), 'defuddle': answers})
        self.assertIn(ENGLISH, raw)
        self.assertNotIn(ARABIC, raw)
        self.assertEqual((entry['durum'], entry['yontem'], entry['dil']), ('cikarildi', 'defuddle', 'en'))
        self.assertEqual(meta['dil'], 'en')
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)
        self.assertEqual(self.track_requests(), [])  # the language matched: no subtitle download at all
        self.assertEqual(len(self.calls('yt-dlp')), 1)
        # An article never costs a yt-dlp request and its card gains no language field.
        page = {'title': 'Yazı', 'language': 'tr', 'content': 'Uzun makale metni. ' * 20}
        entry, meta, _raw = self.isle({'video': dubbed(), 'defuddle': {'': page}}, url='https://ornek.com/yazi')
        self.assertEqual(entry['yontem'], 'defuddle')
        self.assertEqual(self.calls('yt-dlp'), [])
        self.assertNotIn('--lang', self.calls('npx')[0])
        self.assertFalse({'dil', 'altyazi', 'uyari'} & set(meta))

    def test_wrong_defuddle_language_gives_way_to_the_spoken_track(self):
        # Defuddle keeps answering with the Arabic dub even when asked for en-US.
        entry, meta, raw = self.isle({'video': dubbed(), 'defuddle': {'': defuddle('ar', ARABIC)}, 'speech': {'auto:en-orig': ENGLISH}})
        self.assertIn(ENGLISH, raw)
        self.assertNotIn(ARABIC, raw)
        self.assertEqual((entry['yontem'], entry['dil'], entry['altyazi']), ('yt-dlp-altyazi', 'en', 'en-orig (otomatik)'))
        self.assertIn('![](' + VIDEO + ')\n\n## Transcript\n\n**0:01** So, I gave', raw)
        self.assertEqual((meta['dil'], meta['altyazi']), ('en', 'en-orig (otomatik)'))
        self.assertNotIn('uyari', meta)
        self.assertEqual(self.track_requests(), [('en-orig', True)])
        self.assertIn('--lang', self.calls('npx')[0])

    def test_dubbed_video_without_defuddle_requests_one_track_not_every_orig(self):
        entry, meta, raw = self.isle({'video': dubbed(), 'speech': {'auto:en-orig': ENGLISH}}, npx=False)
        self.assertEqual((entry['durum'], entry.get('yontem')), ('cikarildi', 'yt-dlp-altyazi'))
        self.assertIn(ENGLISH, raw)  # not `ar-orig`, the first `-orig` track in the alphabet
        self.assertEqual(self.track_requests(), [('en-orig', True)])  # asking for all 21 is what drew HTTP 429
        self.assertEqual((meta.get('dil'), meta.get('altyazi')), ('en', 'en-orig (otomatik)'))

    def test_http_429_on_the_first_track_request_moves_to_the_next_track(self):
        scenario = {'video': video('tr', audio=[('tr', -1)], manual=['tr'], recognised=['tr-orig', 'tr'], translated=['en']),
                    'speech': {'auto:tr-orig': TURKISH}, 'fail_first': 1}
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertEqual((entry['durum'], entry.get('altyazi')), ('cikarildi', 'tr-orig (otomatik)'))
        self.assertIn(TURKISH, raw)
        self.assertEqual(self.track_requests(), [('tr', False), ('tr-orig', True)])
        self.assertEqual(meta['dil'], 'tr')
        self.assertNotIn('uyari', meta)

    def test_wrong_language_is_kept_only_with_a_warning(self):
        # Every track request is refused: the dub transcript is all there is.
        scenario = {'video': dubbed(), 'defuddle': {'': defuddle('ar', ARABIC)}, 'fail_first': 9}
        entry, meta, raw = self.isle(scenario, whisper=True)
        self.assertEqual((entry['durum'], entry['yontem'], meta.get('dil')), ('cikarildi', 'defuddle', 'ar'))
        self.assertIn(ARABIC, raw)
        for part in ('konusma dilinde olmayabilir', 'video en-US', 'transkript ar'):
            self.assertIn(part, meta.get('uyari', ''))
        self.assertEqual(entry['not'], meta['uyari'])
        self.assertEqual(self.track_requests(), [('en-orig', True)])
        # Audio stays the last resort for a video with no transcript at all; nothing was downloaded here.
        self.assertEqual([call for call in self.calls('yt-dlp') if '-f' in call], [])
        # A later run that gets the track replaces the warning it wrote.
        entry, meta, raw = self.isle(dict(scenario, fail_first=0, speech={'auto:en-orig': ENGLISH}))
        self.assertEqual((meta['dil'], meta['altyazi']), ('en', 'en-orig (otomatik)'))
        self.assertNotIn('uyari', meta)
        self.assertNotIn(ARABIC, raw)

    def test_unknown_language_of_a_single_language_video_is_recorded_without_a_warning(self):
        # Like jNQXAC9IVRw: manual tracks only, no speech recognition, so yt-dlp names no language.
        # Nothing points at a dub, so the stored language on the card is enough.
        scenario = {'video': video(None, audio=[(None, -1)], manual=['de', 'en']), 'defuddle': {'': defuddle('en', ENGLISH)},
                    'speech': {'manual:en': ENGLISH}}
        entry, meta, raw = self.isle(scenario)
        self.assertEqual((entry['yontem'], meta.get('dil')), ('defuddle', 'en'))
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)
        self.assertNotIn('--lang', self.calls('npx')[0])
        self.assertEqual(self.track_requests(), [])
        # Without Defuddle a track is still taken, and the card names it.
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertEqual((entry['yontem'], meta.get('dil'), meta.get('altyazi')), ('yt-dlp-altyazi', 'en', 'en (elle)'))
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)
        self.assertEqual(self.track_requests(), [('en', False)])

    def test_unknown_language_of_a_dubbed_video_warns(self):
        # Two audio languages and neither marked original: whatever is stored may be a dub.
        scenario = {'video': video('tr', audio=[('tr', 5), ('en', -1)], recognised=['en-orig', 'tr-orig'], translated=['de']),
                    'defuddle': {'': defuddle('en', ENGLISH)}}
        entry, meta, raw = self.isle(scenario)
        self.assertEqual((entry['yontem'], meta.get('dil')), ('defuddle', 'en'))
        for part in ('dublajli', 'konusulan dil belirlenemedi', 'transkript dili: en'):
            self.assertIn(part, meta.get('uyari', ''))
        self.assertEqual(entry['not'], meta['uyari'])
        self.assertNotIn('--lang', self.calls('npx')[0])
        # No audio language at all, but speech recognised in two languages: the track taken is named.
        scenario = {'video': video(None, recognised=['en-orig', 'tr-orig']), 'speech': {'auto:tr-orig': TURKISH}}
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertEqual((entry['yontem'], meta.get('dil'), meta.get('altyazi')), ('yt-dlp-altyazi', 'tr', 'tr-orig (otomatik)'))
        for part in ('dublajli', 'kullanilan altyazi izi: tr-orig (otomatik)'):
            self.assertIn(part, meta.get('uyari', ''))
        self.assertEqual(self.track_requests(), [('tr-orig', True)])

    def test_audio_fallback_warns_only_for_a_dubbed_video_of_unknown_language(self):
        # No captions of any kind: the audio is transcribed. With two audio languages and no original it may be a dub.
        scenario = {'video': video(None, audio=[('tr', 5), ('en', -1)]), 'audio': TURKISH}
        entry, meta, raw = self.isle(scenario, npx=False, whisper=True)
        self.assertEqual((entry['durum'], entry.get('yontem')), ('cikarildi', 'ses-whisper'))
        self.assertIn(TURKISH, raw)
        for part in ('dublajli', 'ses asil dilde olmayabilir'):
            self.assertIn(part, meta.get('uyari', ''))
        self.assertNotIn('dil', meta)
        # One audio language: its transcript is the spoken language, nothing to warn about.
        entry, meta, raw = self.isle(dict(scenario, video=video('tr', audio=[('tr', -1)])), npx=False, whisper=True)
        self.assertEqual((entry['yontem'], meta.get('dil'), meta.get('uyari')), ('ses-whisper', 'tr', None))
        self.assertNotIn('not', entry)

    def test_refused_metadata_request_keeps_defuddle_and_asks_for_no_track(self):
        entry, meta, raw = self.isle({'video': None, 'defuddle': {'': defuddle('ar', ARABIC)}})
        self.assertEqual((entry['yontem'], meta.get('dil')), ('defuddle', 'ar'))
        self.assertNotIn('uyari', meta)  # an unanswered request is no evidence of a dub
        self.assertNotIn('not', entry)
        self.assertEqual(self.track_requests(), [])  # no blind request for tracks it could not list
        self.assertEqual(len(self.calls('yt-dlp')), 1)

    def test_turkish_video_manual_track_beats_automatic(self):
        tracks = {'recognised': ['tr-orig', 'tr'], 'translated': ['en', 'ar']}
        scenario = {'video': video('tr', audio=[('tr', -1)], manual=['en', 'tr'], **tracks),
                    'speech': {'manual:tr': TURKISH, 'auto:tr-orig': 'otomatik ' * 20}}
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertIn(TURKISH, raw)  # the manual track, with Ş, ğ, ü, ç, ö, İ and ı intact
        self.assertEqual((meta.get('dil'), meta.get('altyazi')), ('tr', 'tr (elle)'))
        self.assertEqual(self.track_requests(), [('tr', False)])
        # No manual Turkish track: the recognised one, never the English translation or the manual `en`.
        scenario = {'video': video('tr', audio=[('tr', -1)], manual=['en'], **tracks), 'speech': {'auto:tr-orig': TURKISH}}
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertEqual((meta['dil'], meta['altyazi']), ('tr', 'tr-orig (otomatik)'))
        self.assertIn(TURKISH, raw)
        self.assertNotIn('uyari', meta)
        # With Defuddle the Turkish transcript is verified and no track is downloaded.
        entry, meta, raw = self.isle(dict(scenario, defuddle={'tr': defuddle('tr', TURKISH)}))
        self.assertEqual((entry['yontem'], meta['dil'], meta.get('altyazi'), meta.get('uyari')), ('defuddle', 'tr', None, None))
        self.assertEqual(self.track_requests(), [])

    def test_without_ytdlp_defuddle_stands_and_its_language_is_recorded(self):
        entry, meta, raw = self.isle({'defuddle': {'': defuddle('ar', ARABIC)}}, yt_dlp=False)
        self.assertEqual((entry['durum'], entry['yontem']), ('cikarildi', 'defuddle'))
        self.assertIn(ARABIC, raw)  # as before: there is nothing to check it against
        self.assertEqual((meta.get('dil'), entry.get('dil')), ('ar', 'ar'))  # visible on the card, which is enough
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)
        self.assertEqual(self.calls('yt-dlp'), [])
        self.assertNotIn('--lang', self.calls('npx')[0])
        # No transcript and no yt-dlp: the old note, no language claim.
        entry, meta, raw = self.isle({'defuddle': {'': defuddle('en')}}, yt_dlp=False)
        self.assertIn('Altyazi bulunamadi', entry['not'])
        self.assertFalse({'dil', 'altyazi', 'uyari'} & set(meta))

    def test_spoken_language_and_track_order_from_metadata(self):
        def tracks(data):
            with tempfile.TemporaryDirectory() as work, patch.object(yakala, '_run', return_value=(0, json.dumps(data), '')):
                return yakala.youtube_tracks(VIDEO, {'yt_dlp': 'yt-dlp'}, work)
        self.assertEqual(tracks(dubbed()), {'dil': 'en-US', 'dublaj': True, 'adaylar': [('en-orig', True)]})
        # Region codes: the exact track, then the bare language, then another region; a manual one first.
        german = video('de-DE', audio=[('de-DE', -1)], manual=['de', 'en'], recognised=['de-DE-orig', 'de-DE', 'de-orig'], translated=['de'])
        self.assertEqual(tracks(german), {'dil': 'de-DE', 'dublaj': False,
                                          'adaylar': [('de', False), ('de-DE-orig', True), ('de-orig', True)]})
        # No `language`, one recognised track: that is what is spoken.
        self.assertEqual(tracks(video(None, recognised=['ja-orig', 'ja'], translated=['en', 'tr'])),
                         {'dil': 'ja', 'dublaj': False, 'adaylar': [('ja-orig', True), ('ja', True)]})
        # Several audio languages and none marked original: nothing to go on, Turkish and English first.
        unmarked = video('tr', audio=[('tr', 5), ('en', -1), ('en-desc', -10)], recognised=['en-orig', 'tr-orig', 'ar-orig'], translated=['de'])
        self.assertEqual(tracks(unmarked), {'dil': None, 'dublaj': True,
                                            'adaylar': [('tr-orig', True), ('en-orig', True), ('ar-orig', True)]})
        # Evidence of a dub, each kind alone: two audio languages, or speech recognised in two languages.
        self.assertEqual(tracks(video(None, audio=[('tr', 5), ('en', -1)])), {'dil': None, 'dublaj': True, 'adaylar': []})
        self.assertEqual(tracks(video(None, recognised=['en-orig', 'tr-orig'])),
                         {'dil': None, 'dublaj': True, 'adaylar': [('tr-orig', True), ('en-orig', True)]})
        # No evidence: one language throughout. An audio description (`en-desc`) and a second recognised
        # track of the same language are not dubs, and neither is a video that names no language at all.
        described = video(None, audio=[('en', -1), ('en-desc', -10)], recognised=['en-orig', 'en-US-orig'])
        self.assertEqual(tracks(described), {'dil': None, 'dublaj': False, 'adaylar': [('en-US-orig', True), ('en-orig', True)]})
        self.assertEqual(tracks(video(None, audio=[(None, -1)], manual=['de', 'en'])),
                         {'dil': None, 'dublaj': False, 'adaylar': [('en', False), ('de', False)]})
        # A language that is not a language code is not passed on to a command line.
        self.assertIsNone(tracks(video('--evil', recognised=['en-orig', 'tr-orig']))['dil'])
        odd = video('en', manual=['live_chat', 'all', '-en', 'en.*', 'en'], recognised=['en-orig'])
        self.assertEqual(tracks(odd)['adaylar'], [('en', False), ('en-orig', True)])
        # A playlist address answers with its videos; the first one stands for it.
        self.assertEqual(tracks({'_type': 'playlist', 'entries': [dubbed(), german]})['dil'], 'en-US')
        for unusable in (['not', 'a', 'video'], {'_type': 'playlist', 'entries': []}):
            self.assertIsNone(tracks(unusable))
        self.assertIsNone(yakala.youtube_tracks(VIDEO, {'yt_dlp': None}, self.tmp.name))


class YakalaInstalledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / 'Örnek Beyin', root / 'state'
        self.vault.mkdir()
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                                 HOME=str(root / 'home'), APPDATA=str(root / 'appdata'))
        self.env.pop('BEYIN_V3_FILTER_HARNESS_TURNS', None)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
                                env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def entry(self, *args):
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', *args], capture_output=True,
                                text=True, encoding='utf-8', env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def session_start(self):
        payload = {'hook_event_name': 'SessionStart', 'session_id': 's1', 'event_id': 's1-1', 'cwd': str(self.vault)}
        result = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault',
                                 str(self.vault), '--state', str(self.state), '--harness', 'claude'],
                                input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
                                env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_installed_module_and_entry(self):
        self.assertTrue((self.vault / '.claude/scripts/beyin_v3_yakala.py').is_file())
        added = self.entry('ekle', 'https://x.com/a/status/7', '--neden', 'okunacak')
        self.assertEqual(added['kaynak_turu'], 'x')
        listed = self.entry('liste')
        self.assertEqual(listed['bekleyen'], 1)

    @unittest.skipUnless(os.name == 'posix', 'the stand-in tools are shell wrappers on PATH')
    def test_isle_stores_the_spoken_language_of_a_dubbed_video(self):
        tools = Path(self.tmp.name) / 'bin'
        tools.mkdir()
        (tools / 'fake_tool.py').write_text(FAKE_TOOL, encoding='utf-8')
        scenario = {'video': dubbed(), 'defuddle': {'': defuddle('ar', ARABIC)}, 'speech': {'auto:en-orig': ENGLISH}}
        (tools / 'scenario.json').write_text(json.dumps(scenario), encoding='utf-8')
        for name in ('npx', 'yt-dlp'):
            (tools / name).write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(tools / 'fake_tool.py'), name])
                                      + ' "$@"\n', encoding='utf-8')
            (tools / name).chmod(0o755)
        self.env['PATH'] = isolated_path(tools)
        added = self.entry('ekle', VIDEO, '--neden', 'dublaj')
        entry = self.entry('isle', '--ses-yok')['kartlar'][0]
        self.assertEqual((entry['durum'], entry.get('yontem'), entry.get('dil')), ('cikarildi', 'yt-dlp-altyazi', 'en'))
        meta = yakala.find_card(self.vault, added['id'])['meta']
        self.assertEqual((meta.get('dil'), meta.get('altyazi'), meta.get('uyari')), ('en', 'en-orig (otomatik)', None))
        raw = (self.vault / entry['ham']).read_text(encoding='utf-8')
        self.assertIn(ENGLISH, raw)
        self.assertNotIn(ARABIC, raw)
        calls = [json.loads(line) for line in (tools / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        asked = [call[call.index('--sub-langs') + 1] for call in calls if '--sub-langs' in call]
        self.assertEqual(asked, ['en-orig'])
        self.assertEqual([call[0] for call in calls], ['yt-dlp', 'npx', 'yt-dlp'])

    def test_session_notice_is_opt_in(self):
        self.entry('ekle', 'https://ornek.com/yazi')
        self.assertNotIn('Yakalanan', self.session_start())
        installed = self.entry('kur', '--kisayol-yok')
        self.assertIsNone(installed['kisayol'])
        self.assertTrue((self.state / 'yakala.json').is_file())
        skill = self.vault / '.agents/skills/beyin-yakala/SKILL.md'
        self.assertIn('python3 beyin.py yakala isle', skill.read_text(encoding='utf-8'))
        self.assertTrue((self.vault / installed['web_clipper_sablonu']).is_file())
        self.assertIn('Yakalanan 1 kaynak bekliyor', self.session_start())
        removed = self.entry('kaldir')
        self.assertTrue(removed['notlar_korundu'])
        self.assertFalse(skill.exists())
        self.assertFalse((self.state / 'yakala.json').exists())
        self.assertEqual(len(list((self.vault / INBOX).glob('*.md'))), 1)
        self.assertNotIn('Yakalanan', self.session_start())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows Start menu and Send To shortcuts')
    def test_windows_shortcuts(self):
        appdata = Path(self.env['APPDATA'])
        installed = self.entry('kur')
        self.assertEqual(installed['kisayol'], 'Ctrl+Alt+B')
        start = appdata / 'Microsoft/Windows/Start Menu/Programs/Beyne At.lnk'
        sendto = appdata / 'Microsoft/Windows/SendTo/Beyne At.lnk'
        self.assertTrue(start.is_file() and sendto.is_file())
        self.assertIsInstance(yakala.windows_context(), dict)
        self.entry('kaldir')
        self.assertFalse(start.exists() or sendto.exists())

    def test_user_skill_with_same_name_is_kept(self):
        own = self.vault / '.agents/skills/beyin-yakala/SKILL.md'
        own.parent.mkdir(parents=True)
        own.write_text('kendi skill\'im\n', encoding='utf-8')
        self.entry('kur', '--kisayol-yok')
        self.entry('kaldir')
        self.assertEqual(own.read_text(encoding='utf-8'), 'kendi skill\'im\n')


if __name__ == '__main__':
    unittest.main()
