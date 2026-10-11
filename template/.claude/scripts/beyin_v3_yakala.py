#!/usr/bin/env python3
"""Optional capture ("yakala"): a one-key popup, a browser clipper template and a source queue.

Capturing never touches the network: a card is one Markdown file in the inbox folder.
`isle` is the only step that fetches, and only for cards the user captured. Nothing here
runs unless the user calls it; `kur` is the explicit opt-in for the hotkey and the
SessionStart notice (state/yakala.json, outside the preferences schema so rollback stays safe).
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

sys.dont_write_bytecode = True

DEFAULT_INBOX = '📥 000-Inbox/Yakala'
INBOX = DEFAULT_INBOX
STARTER, CARDS = DEFAULT_INBOX.split('/')
RAW = '.ham'
FILES = 'dosyalar'
STATE_FILE = 'yakala.json'
DEFUDDLE = 'defuddle@0.19.4'  # pinned: npx runs exactly this release
LAUNCH_LABEL = 'com.avenoxbeyin.yakala'
MAC_HOTKEY = 'Control+Option+B'  # Option+Command+B is the bookmarks key in Chrome and Safari
WIN_HOTKEY = 'CTRL+ALT+B'
STATUSES = ('bekliyor', 'cikarildi', 'islendi', 'hata')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ---------------------------------------------------------------- cards

# The doctor's inbox report tells an inbox folder by these words on a folded name
# (beyin_v3_hygiene: INBOX_WORDS, SENSITIVE_WORDS, fold). Copied, not imported: the hotkey
# listener runs this file alone from the state folder. A test keeps the copies equal.
INBOX_WORDS = re.compile(r'(?:inbox|inboxes|gelenkutusu|gelenkutum)')
SENSITIVE_WORDS = re.compile(
    r'(?:kasa|sifre|parola|kimlik|kimlig|finans|finansal|musteri|vergi|fatura|maas|ozel|gizli|gizlilik)'
    r'(?:ler|lar)?(?:i|im|in|imiz|leri|lari)?'
    r'|(?:private|secret|password|credential)s?')
ARCHIVE_WORDS = re.compile(r'(?:arsiv|archive)\w*')  # any suffix: "Inbox Arşivi", "Archived"
_TR_FOLD = str.maketrans({'ş': 's', 'ğ': 'g', 'ü': 'u', 'ö': 'o', 'ç': 'c', 'ı': 'i', 'â': 'a', 'î': 'i', 'û': 'u'})


def _name_words(name):
    """Letter-only words of a name after NFC, Turkish İ/ı and ASCII folding ('GELEN KUTUSU', NFD 'Arşiv')."""
    text = unicodedata.normalize('NFC', str(name)).replace('İ', 'i').replace('I', 'i').lower()
    return re.findall(r'[^\W\d_]+', unicodedata.normalize('NFC', text.replace('\u0307', '')).translate(_TR_FOLD))


def _inbox_name(name):
    """An inbox by word ('000-Inbox', '00_INBOX', 'Gelen Kutusu'); never an archive or a kasa-class folder."""
    words = _name_words(name)
    if any(SENSITIVE_WORDS.fullmatch(word) or ARCHIVE_WORDS.fullmatch(word) for word in words):
        return False
    return any(INBOX_WORDS.fullmatch(word) for word in words) or any(
        pair in (('gelen', 'kutusu'), ('gelen', 'kutum')) for pair in zip(words, words[1:]))


def chosen_inbox(vault, spec):
    """A folder the user named (`kur --klasor`) as a vault-relative path; ValueError unless it stays inside the vault.

    Resolved first, so '..', an absolute path and a link that leaves the vault are all caught
    before anything is created.
    """
    vault = Path(vault).resolve()
    text = str(spec or '').strip().replace('\\', '/')
    target = (vault / text).resolve() if text else vault
    if target == vault or vault not in target.parents:
        raise ValueError('Klasor vault\'un icinde olmali: ' + str(spec))
    relative = target.relative_to(vault)
    if any(part.startswith('.') for part in relative.parts):
        raise ValueError('Nokta ile baslayan klasor secilemez (Obsidian gostermez): ' + str(spec))
    if target.exists() and not target.is_dir():
        raise ValueError('Bu bir klasor degil: ' + str(spec))
    return relative.as_posix()


def _same_folder(first, second):
    try:
        return first == second or os.path.samefile(first, second)
    except OSError:
        return False


def _saved_inbox(vault, state=None):
    """The folder `kur` wrote to yakala.json, while it is still a folder inside the vault."""
    try:
        saved = json.loads(_state_path(state or resolve_state(vault)).read_text(encoding='utf-8')).get('klasor')
        if saved and isinstance(saved, str):
            relative = chosen_inbox(vault, saved)
            return relative if (Path(vault) / relative).is_dir() else None
    except (OSError, ValueError, AttributeError):
        pass
    return None


def find_inbox(vault, state=None):
    """Vault-relative folder that holds the cards; cards that already exist outrank a name.

    The folder `kur` saved wins while it exists. Otherwise, when exactly one inbox (the starter
    folder '📥 000-Inbox' included) already holds 'Yakala/', that one; else the starter folder
    when the vault has it; else the only top-level folder named like an inbox. Anything less
    certain is not guessed: the starter path is used, as before. An inbox is told by word; dot
    folders, links, archives and kasa-class names are never picked.
    """
    vault = Path(vault)
    saved = _saved_inbox(vault, state)
    if saved:
        return saved
    if (vault / DEFAULT_INBOX).is_dir():
        return DEFAULT_INBOX  # in use: alone it is the one, beside another used inbox the starter wins
    try:
        with os.scandir(vault) as entries:
            names = sorted(entry.name for entry in entries if not entry.name.startswith('.') and
                           not entry.is_symlink() and entry.is_dir() and _inbox_name(entry.name))
    except OSError:
        names = []
    used = [name for name in names if (vault / name / CARDS).is_dir()]
    if len(used) == 1:
        return used[0] + '/' + CARDS
    if (vault / STARTER).is_dir():
        return DEFAULT_INBOX
    return names[0] + '/' + CARDS if len(names) == 1 else DEFAULT_INBOX


def inbox(vault, state=None):
    return Path(vault) / find_inbox(vault, state)


def now():
    return datetime.now().astimezone()


def slug(text, limit=48):
    text = str(text or '').replace('ı', 'i').replace('İ', 'I')
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode('ascii').lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return (text[:limit].rstrip('-') or 'not')


def kind_of(url=None, files=(), app=None):
    if files:
        return 'dosya'
    if not url:
        return 'mail' if app and re.search(r'mail|outlook|spark|thunderbird', app, re.I) else 'metin'
    host = (urlparse(url).hostname or '').lower().removeprefix('www.').removeprefix('m.')
    path = urlparse(url).path.lower()
    if host in ('youtube.com', 'youtu.be', 'music.youtube.com'):
        return 'youtube'
    if host in ('x.com', 'twitter.com', 'fxtwitter.com', 'vxtwitter.com'):
        return 'x'
    if host in ('mail.google.com', 'outlook.live.com', 'outlook.office.com', 'mail.proton.me'):
        return 'mail'
    if host == 'github.com':
        return 'github'
    if path.endswith('.pdf'):
        return 'pdf'
    return 'makale'


def canonical(url):
    """Same video or page captured twice must land on one card."""
    if not url:
        return ''
    parts = urlparse(url.strip())
    host = (parts.hostname or '').lower().removeprefix('www.').removeprefix('m.')
    if host == 'youtu.be':
        return 'youtube:' + parts.path.strip('/')
    if host.endswith('youtube.com'):
        video = parse_qs(parts.query).get('v', [''])[0] or parts.path.rsplit('/', 1)[-1]
        return 'youtube:' + video
    if host in ('x.com', 'twitter.com'):
        match = re.search(r'/status/(\d+)', parts.path)
        if match:
            return 'x:' + match.group(1)
    return host + parts.path.rstrip('/') + ('?' + parts.query if parts.query else '')


def _value(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    if raw.startswith("'") and raw.endswith("'") and len(raw) > 1:
        return raw[1:-1].replace("''", "'")
    return raw


def read_card(path):
    text = Path(path).read_text(encoding='utf-8')
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        match = re.match(r'^([A-Za-z_][\w-]*):\s?(.*)$', line)
        if match:
            meta[match.group(1)] = _value(match.group(2))
    return meta, text[end + 4:].lstrip('\n')


def _dump(meta, body):
    lines = ['---']
    for key, value in meta.items():
        if value is None or value == '':
            continue
        lines.append(key + ': ' + json.dumps(value, ensure_ascii=False))
    return '\n'.join(lines) + '\n---\n\n' + body.rstrip() + '\n'


def write_card(path, meta, body):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix='.yakala-', dir=path.parent)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(_dump(meta, body))
    os.replace(temp, path)


def cards(vault, state=None, folder=None):
    folder = Path(folder) if folder else inbox(vault, state)
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob('*.md')):
        try:
            meta, body = read_card(path)
        except (OSError, UnicodeDecodeError):
            continue
        if meta.get('tur') == 'yakala':
            found.append({'path': path, 'id': path.stem, 'meta': meta, 'body': body})
    return found


def find_card(vault, card_id, state=None):
    for card in cards(vault, state):
        if card['id'] == card_id or card['path'].name == card_id:
            return card
    raise ValueError('Kart bulunamadi: ' + str(card_id))


def pending(vault, state=None):
    """Cheap for SessionStart: one directory listing, a 600-byte head read per card."""
    folder = inbox(vault, state)
    count = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        if entry.name.endswith('.md') and entry.is_file():
            try:
                with open(entry.path, encoding='utf-8', errors='ignore') as handle:
                    head = handle.read(600)
            except OSError:
                continue
            if re.search(r'^durum:\s*"?bekliyor"?\s*$', head, re.M) and re.search(r'^tur:\s*"?yakala"?\s*$', head, re.M):
                count += 1
    return count


def _local_only(folder):
    """Raw text and captured files stay out of a versioned vault, whichever folder holds the cards."""
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / '.gitignore'
    if not ignore.exists():
        try:
            ignore.write_text('*\n', encoding='utf-8', newline='\n')
        except OSError:
            pass
    return folder


def _unique(path):
    path = Path(path)
    if not path.exists():
        return path
    for number in range(2, 1000):
        candidate = path.with_name(path.stem + '-' + str(number) + path.suffix)
        if not candidate.exists():
            return candidate
    raise ValueError('Ayni adla cok fazla dosya var: ' + path.name)


def capture(vault, url=None, text=None, files=(), why='', app=None, title=None, tool='masaustu', state=None):
    """Write one card; a repeated URL only gains the new reason. No network."""
    vault = Path(vault)
    url = (url or '').strip() or None
    text = (text or '').strip() or None
    why = (why or '').strip()
    files = [Path(f).expanduser() for f in files]
    if not (url or text or files):
        raise ValueError('Yakalanacak bir sey yok: URL, metin ya da dosya ver.')
    folder = inbox(vault, state)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now()
    if url:
        key = canonical(url)
        for card in cards(vault, folder=folder):
            if card['meta'].get('url') and canonical(card['meta']['url']) == key:
                body = card['body'].rstrip()
                addition = '\n\n## Yeniden yakalandi (' + stamp.strftime('%Y-%m-%d %H:%M') + ')\n' + (why or '_(not yok)_')
                if text:
                    addition += '\n\n' + _quote(text)
                write_card(card['path'], card['meta'], body + addition)
                return {'status': 'mevcut_karta_eklendi', 'id': card['id'], 'path': str(card['path'].relative_to(vault))}
    kind = kind_of(url, files, app)
    if not title:
        title = (files[0].name if files else None) or (url if url else _first_line(text))
    copied = []
    for source in files:
        if not source.is_file():
            raise ValueError('Dosya bulunamadi: ' + str(source))
        target = _unique(_local_only(folder / FILES) / source.name)
        shutil.copy2(source, target)
        copied.append(target.relative_to(vault).as_posix())
    name = stamp.strftime('%Y-%m-%d-%H%M') + '-' + (url_slug(url) if url and title == url else slug(title))
    path = _unique(folder / (name + '.md'))
    meta = {'tur': 'yakala', 'durum': 'bekliyor', 'kaynak_turu': kind, 'baslik': _first_line(title, 160),
            'url': url, 'yakalandi': stamp.isoformat(timespec='seconds'), 'arac': tool, 'uygulama': app,
            'visibility': 'private' if kind in ('mail', 'metin', 'dosya') else 'internal'}
    body = ['# ' + _first_line(title, 160), '', '## Neden', '', why or '_(not yok)_']
    if url:
        body += ['', '## Kaynak', '', url]
    if text:
        body += ['', '## Secim', '', _quote(text)]
    if copied:
        body += ['', '## Dosyalar', ''] + ['- [[' + item + ']]' for item in copied]
    write_card(path, meta, '\n'.join(body))
    return {'status': 'yakalandi', 'id': path.stem, 'kaynak_turu': kind, 'path': path.relative_to(vault).as_posix()}


def url_slug(url):
    """Readable file name before the real title is known: youtube-<id>, x-<user>-<id>, site-last-segment."""
    key = canonical(url)
    if key.startswith('youtube:'):
        return 'youtube-' + slug(key[8:], 20)
    parts = urlparse(url)
    if key.startswith('x:'):
        return slug('x-' + parts.path.strip('/').split('/')[0] + '-' + key[2:])
    host = (parts.hostname or 'link').lower().removeprefix('www.').split('.')[0]
    tail = next((seg for seg in reversed(parts.path.split('/')) if seg), '')
    return slug(host + '-' + re.sub(r'\.[a-z0-9]{2,5}$', '', tail))


def _first_line(text, limit=80):
    line = next((part.strip() for part in str(text or '').splitlines() if part.strip()), '')
    return line[:limit] or 'Not'


def _quote(text):
    return '\n'.join('> ' + line if line else '>' for line in str(text).strip().splitlines())


# ---------------------------------------------------------------- extraction

def _which(*names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _run(command, timeout, cwd=None):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', NO_COLOR='1')
    result = subprocess.run(command, capture_output=True, timeout=timeout, cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    return result.returncode, result.stdout.decode('utf-8', 'replace'), result.stderr.decode('utf-8', 'replace')


def tools():
    """Optional helpers; each one only widens what `isle` can do."""
    whisper = _which('mlx_whisper', 'whisper-ctranslate2', 'whisper')
    return {'npx': _which('npx'), 'yt_dlp': _which('yt-dlp', 'yt_dlp'), 'whisper': whisper,
            'pdftotext': _which('pdftotext'), 'ffmpeg': _which('ffmpeg')}


def via_defuddle(url, found, info=None):
    """Main text as Markdown; title, author and date go into `info` when given."""
    if not found['npx']:
        return None
    try:
        code, out, _ = _run([found['npx'], '-y', DEFUDDLE, 'parse', url, '--json', '--markdown'], timeout=180)
        data = json.loads(out) if code == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if info is not None:
        info.update({key: str(data[key]).strip() for key in ('title', 'author', 'published', 'site')
                     if data.get(key) and str(data[key]).strip()})
    text = str(data.get('content') or '').strip()
    return text if len(text) > 80 else None


def vtt_text(raw):
    """WebVTT to paragraphs that start with a timestamp roughly every 30 seconds; rolling duplicates removed."""
    paragraphs, last, mark, stamp = [], None, -999, None
    for line in raw.splitlines():
        line = line.strip()
        match = re.match(r'^(?:(\d+):)?(\d+):(\d+)[.,]\d+\s+-->', line)
        if match:
            hours, minutes, seconds = (int(part or 0) for part in match.groups())
            stamp = hours * 3600 + minutes * 60 + seconds
            continue
        if not line or line.startswith(('WEBVTT', 'Kind:', 'Language:', 'NOTE')) or line.isdigit() or '-->' in line:
            continue
        line = re.sub(r'<[^>]+>', '', line).strip()
        if not line or line == last:
            continue
        last = line
        if stamp is not None and stamp - mark >= 30 or not paragraphs:
            mark = stamp if stamp is not None else mark
            label = '%d:%02d' % divmod(stamp or 0, 60) if (stamp or 0) < 3600 else '%d:%02d:%02d' % (stamp // 3600, stamp % 3600 // 60, stamp % 60)
            paragraphs.append(['**' + label + '**', line])
        else:
            paragraphs[-1].append(line)
    return '\n\n'.join(' '.join(words) for words in paragraphs).strip()


def via_subtitles(url, found, workdir):
    if not found['yt_dlp']:
        return None
    command = [found['yt_dlp'], '--skip-download', '--write-subs', '--write-auto-subs',
               '--sub-langs', '.*-orig,tr,en', '--sub-format', 'vtt', '--no-playlist',
               '-o', str(Path(workdir) / 'sub.%(ext)s'), url]
    try:
        _run(command, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    def rank(path):  # original-language track first, then a human Turkish/English one
        name = path.name
        return (0 if name.endswith('-orig.vtt') else 1 if name.endswith('.tr.vtt') else 2 if name.endswith('.en.vtt') else 3, name)
    subs = sorted(Path(workdir).glob('sub*.vtt'), key=rank)
    for path in subs:
        text = vtt_text(path.read_text(encoding='utf-8', errors='replace'))
        if len(text) > 80:
            return text
    return None


def via_audio(url, found, workdir):
    """Last resort: download only the audio, transcribe locally, delete the audio."""
    if not (found['yt_dlp'] and found['whisper']):
        return None
    audio = Path(workdir) / 'ses'
    try:
        _run([found['yt_dlp'], '-f', 'bestaudio/best', '--no-playlist', '-o', str(audio) + '.%(ext)s', url], timeout=900)
        files = sorted(Path(workdir).glob('ses.*'))
        if not files:
            return None
        whisper = Path(found['whisper']).name.lower()
        if whisper.startswith('mlx_whisper'):
            command = [found['whisper'], str(files[0]), '--model', 'mlx-community/whisper-large-v3-turbo',
                       '-f', 'txt', '-o', str(workdir)]
        else:
            command = [found['whisper'], str(files[0]), '--output_format', 'txt', '--output_dir', str(workdir)]
        _run(command, timeout=7200)
        texts = sorted(Path(workdir).glob('ses*.txt'))
        return texts[0].read_text(encoding='utf-8', errors='replace').strip() if texts else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        for path in Path(workdir).glob('ses.*'):
            if path.suffix != '.txt':
                path.unlink(missing_ok=True)


def via_stdlib(url):
    """No helper installed: plain HTTP fetch and a rough HTML-to-text."""
    from html.parser import HTMLParser
    from urllib.request import Request, urlopen

    class Text(HTMLParser):
        skip = {'script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg', 'form', 'aside'}

        def __init__(self):
            super().__init__()
            self.parts, self.depth, self.title, self.in_title = [], 0, '', False

        def handle_starttag(self, tag, attrs):
            if tag in self.skip:
                self.depth += 1
            if tag == 'title':
                self.in_title = True
            if tag in ('p', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'tr', 'div', 'section', 'article'):
                self.parts.append('\n')

        def handle_endtag(self, tag):
            if tag in self.skip and self.depth:
                self.depth -= 1
            if tag == 'title':
                self.in_title = False

        def handle_data(self, data):
            if self.in_title:
                self.title += data
            elif not self.depth:
                self.parts.append(data)

    try:
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (beyin-yakala)'})
        with urlopen(request, timeout=30) as response:
            if 'html' not in response.headers.get('Content-Type', 'text/html'):
                return None
            html = response.read(5_000_000).decode(response.headers.get_content_charset() or 'utf-8', 'replace')
    except Exception:
        return None
    parser = Text()
    parser.feed(html)
    text = re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[ \t]+', ' ', ''.join(parser.parts))).strip()
    return (('# ' + parser.title.strip() + '\n\n') if parser.title.strip() else '') + text if len(text) > 200 else None


def _section(body, name):
    match = re.search(r'^## ' + name + r'\s*$(.*?)(?=^## |\Z)', body, re.M | re.S)
    return match.group(1).strip() if match else ''


def _without_section(body, name, replacement):
    return re.sub(r'^## ' + name + r'\s*$.*?(?=^## |\Z)', '## ' + name + '\n\n' + replacement + '\n\n', body, count=1, flags=re.M | re.S)


def extract(vault, card, found, allow_audio=True, info=None):
    """Return (text, method, note). Prefer what is already captured, then the cheapest fetch."""
    meta, body = card['meta'], card['body']
    url, kind = meta.get('url'), meta.get('kaynak_turu') or kind_of(meta.get('url'))
    clipped = _section(body, 'Icerik') or _section(body, 'İçerik')
    if clipped and len(clipped) > 200 and kind != 'youtube':
        return clipped, 'web-clipper', None
    if kind == 'youtube' and url:
        text = via_defuddle(url, found, info)
        if text and re.search(r'^## Transcript', text, re.M):
            return text, 'defuddle', None
        with tempfile.TemporaryDirectory(prefix='beyin-yakala-') as workdir:
            subtitles = via_subtitles(url, found, workdir)
            if subtitles:
                return ((text + '\n\n## Transcript\n\n') if text else '') + subtitles, 'yt-dlp-altyazi', None
            if allow_audio:
                spoken = via_audio(url, found, workdir)
                if spoken:
                    return ((text + '\n\n## Transcript\n\n') if text else '') + spoken, 'ses-whisper', None
        if text:
            return text, 'defuddle', 'Altyazi bulunamadi; yalniz baslik ve aciklama var. Ses yolu icin yt-dlp ve whisper gerekir.'
        return None, None, 'YouTube icerigi alinamadi: npx (Node), yt-dlp ya da whisper bulunamadi veya video erisilemez.'
    if kind == 'mail':
        selected = _section(body, 'Secim')
        if selected:
            return selected, 'secim', 'Mailin tamami icin ajan, varsa Gmail/Outlook baglantisiyla thread\'i okusun.'
        return None, None, 'Mail icerigi giris gerektiriyor; Web Clipper ile mail acikken yakala ya da metni secip yakala.'
    if kind == 'metin':
        return _section(body, 'Secim') or body, 'metin', None
    if kind == 'dosya':
        parts = []
        for link in re.findall(r'\[\[([^\]]+)\]\]', _section(body, 'Dosyalar')):
            path = Path(vault) / link
            if path.suffix.lower() in ('.md', '.txt', '.csv', '.json', '.html', '.srt', '.vtt'):
                parts.append('## ' + path.name + '\n\n' + path.read_text(encoding='utf-8', errors='replace'))
            elif path.suffix.lower() == '.pdf' and found['pdftotext']:
                try:
                    code, out, _ = _run([found['pdftotext'], '-layout', str(path), '-'], timeout=120)
                    if code == 0 and out.strip():
                        parts.append('## ' + path.name + '\n\n' + out)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if parts:
            return '\n\n'.join(parts), 'dosya', None
        return None, 'ajan-okusun', 'Dosyayi ajan dogrudan okusun (gorsel, PDF ya da ikili dosya).'
    if url:
        text = via_defuddle(url, found, info)
        if text:
            return text, 'defuddle', None
        text = via_stdlib(url)
        if text:
            return text, 'http', None
        return None, None, 'Sayfa alinamadi (giris gerekebilir); Web Clipper ile sayfa acikken yakala.'
    return _section(body, 'Secim') or None, 'metin', None


def process(vault, card_ids=None, allow_audio=True, retry=False, state=None):
    vault = Path(vault)
    found = tools()
    targets = [find_card(vault, cid, state) for cid in card_ids] if card_ids else \
        [c for c in cards(vault, state) if c['meta'].get('durum') == 'bekliyor' or (retry and c['meta'].get('durum') == 'hata')]
    results = []
    for card in targets:
        meta, body = dict(card['meta']), card['body']
        try:
            info = {}
            text, method, note = extract(vault, card, found, allow_audio, info)
        except Exception as exc:  # one broken source must not stop the queue
            text, method, note, info = None, None, type(exc).__name__ + ': ' + str(exc)[:200], {}
        entry = {'id': card['id'], 'kart': card['path'].relative_to(vault).as_posix(),
                 'kaynak_turu': meta.get('kaynak_turu'), 'baslik': meta.get('baslik'),
                 'neden': _section(body, 'Neden'), 'url': meta.get('url')}
        if info.get('title') and (not meta.get('baslik') or str(meta.get('baslik')).startswith(('http://', 'https://'))):
            body = re.sub(r'^# .*$', lambda _m: '# ' + _first_line(info['title'], 160), body, count=1, flags=re.M)
            meta['baslik'] = entry['baslik'] = _first_line(info['title'], 160)
        meta.update({target: info[source] for source, target in (('author', 'yazar'), ('published', 'yayin'), ('site', 'site'))
                     if info.get(source)})
        if text:
            raw = _local_only(card['path'].parent / RAW) / (card['id'] + '.md')
            header = ['---', 'tur: yakala-ham', 'kart: ' + json.dumps(entry['kart'], ensure_ascii=False),
                      'baslik: ' + json.dumps(meta.get('baslik') or '', ensure_ascii=False),
                      'url: ' + json.dumps(meta.get('url') or '', ensure_ascii=False),
                      'yontem: ' + json.dumps(method), 'cikarildi: ' + json.dumps(now().isoformat(timespec='seconds')),
                      'visibility: private', '---', '']
            raw.write_text('\n'.join(header) + text.strip() + '\n', encoding='utf-8', newline='\n')
            if method == 'web-clipper':
                body = _without_section(body, 'Icerik' if _section(body, 'Icerik') else 'İçerik',
                                        'Tam metin ham dosyaya tasindi: `' + raw.relative_to(vault).as_posix() + '`')
            meta.update(durum='cikarildi', yontem=method, ham=raw.relative_to(vault).as_posix())
            meta.pop('hata', None)
            entry.update(durum='cikarildi', yontem=method, ham=meta['ham'], karakter=len(text))
        elif method == 'ajan-okusun':
            meta.update(durum='cikarildi', yontem=method)
            entry.update(durum='cikarildi', yontem=method)
        else:
            meta.update(durum='hata', hata=note or 'icerik alinamadi')
            entry.update(durum='hata')
        if note:
            entry['not'] = note
        write_card(card['path'], meta, body)
        results.append(entry)
    return {'status': 'tamam', 'islenen': len(results), 'kartlar': results,
            'araclar': {name: bool(path) for name, path in found.items()}}


def finish(vault, card_id, sources=(), summary=None, state=None):
    vault = Path(vault)
    card = find_card(vault, card_id, state)
    meta, body = dict(card['meta']), card['body'].rstrip()
    links = []
    for source in sources:
        source = str(source).replace('\\', '/')
        if not (vault / source).is_file():
            raise ValueError('Bilgi notu bulunamadi: ' + source)
        links.append('- [[' + source.removesuffix('.md') + ']]')
    meta.update(durum='islendi', islendi=now().isoformat(timespec='seconds'))
    addition = '\n\n## Islendi\n\n' + ((summary.strip() + '\n\n') if summary else '') + '\n'.join(links or ['_(bilgi notu yok)_'])
    write_card(card['path'], meta, body + addition)
    return {'status': 'islendi', 'id': card['id'], 'bilgi': sources}


def listing(vault, status=None, state=None):
    rows = []
    for card in cards(vault, state):
        meta = card['meta']
        if status and meta.get('durum') != status:
            continue
        rows.append({'id': card['id'], 'durum': meta.get('durum'), 'kaynak_turu': meta.get('kaynak_turu'),
                     'baslik': meta.get('baslik'), 'url': meta.get('url'), 'ham': meta.get('ham')})
    return {'status': 'tamam', 'kartlar': rows, 'bekleyen': sum(1 for r in rows if r['durum'] == 'bekliyor')}


def session_notice(vault, state=None):
    count = pending(vault, state)
    if not count:
        return ''
    folder = find_inbox(vault, state)
    return ('Yakalanan ' + str(count) + ' kaynak bekliyor (' + folder + '). Kullanici isterse beyin-yakala skill\'iyle isle; '
            'kendiliginden baslama.\n')


# ---------------------------------------------------------------- context (which app, which page)

def _objc():
    import ctypes
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def send(target, selector, restype=ctypes.c_void_p, *args):
        argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [type(a) for a in args]
        function = ctypes.CFUNCTYPE(restype, *argtypes)(('objc_msgSend', objc))
        return function(target, objc.sel_registerName(selector), *args)
    return objc, send


def mac_frontmost():
    import ctypes
    try:
        objc, send = _objc()
        workspace = send(objc.objc_getClass(b'NSWorkspace'), b'sharedWorkspace')
        app = send(workspace, b'frontmostApplication')

        def string(value):
            raw = send(value, b'UTF8String', ctypes.c_char_p) if value else None
            return raw.decode('utf-8') if raw else None
        return {'uygulama': string(send(app, b'localizedName')), 'bundle': string(send(app, b'bundleIdentifier'))}
    except Exception:
        return {}


CHROMIUM = {'com.google.Chrome', 'com.google.Chrome.canary', 'com.brave.Browser', 'com.microsoft.edgemac',
            'company.thebrowser.Browser', 'com.vivaldi.Vivaldi', 'com.operasoftware.Opera'}


def _osascript(script, timeout=2.5):
    try:
        result = subprocess.run(['osascript', '-e', script], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return result.stdout.decode('utf-8', 'replace').strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def mac_context(front=None):
    front = front or mac_frontmost()
    bundle = front.get('bundle') or ''
    context = dict(front)
    if bundle in CHROMIUM:
        out = _osascript('tell application id "' + bundle + '" to return (URL of active tab of front window) & linefeed & (title of active tab of front window)')
    elif bundle in ('com.apple.Safari', 'com.apple.SafariTechnologyPreview'):
        out = _osascript('tell application id "' + bundle + '" to return (URL of front document) & linefeed & (name of front document)')
    else:
        out = None
    if out:
        url, _, title = out.partition('\n')
        if url.startswith(('http://', 'https://')):
            context.update(url=url.strip(), baslik=title.strip())
    if bundle == 'com.apple.finder':
        out = _osascript('tell application "Finder"\nset out to ""\nrepeat with f in (selection as alias list)\n'
                         'set out to out & POSIX path of f & linefeed\nend repeat\nreturn out\nend tell')
        paths = [line for line in (out or '').splitlines() if line and Path(line).is_file()]
        if paths:
            context['dosyalar'] = paths[:20]
    return context


def windows_context():
    import ctypes
    from ctypes import wintypes
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        window = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(window)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)
        name = None
        if process:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                name = Path(path.value).stem
            kernel32.CloseHandle(process)
        return {'uygulama': name, 'pencere': buffer.value}
    except Exception:
        return {}


def _try(command, timeout=5):
    """(returncode, stdout); (None, '') when the tool is missing or hangs."""
    try:
        code, out, _ = _run(command, timeout)
        return code, out
    except (OSError, subprocess.TimeoutExpired):
        return None, ''


def _linux_clipboard():
    """Clipboard text through one helper: wl-paste on Wayland (the reader the window uses), else xclip or xsel."""
    text = _wl_paste()
    if text is None:
        for name, options in (('xclip', ['-o', '-selection', 'clipboard']), ('xsel', ['-ob'])):
            tool = _which(name)
            if tool:
                text = _tool_text([tool] + options)
                break
    return repair_mojibake(text or '')


def _file_uri_path(line):
    """Path of a local file:// URI; None for other text and for a URI that names another machine."""
    match = re.fullmatch(r'file://(?:localhost)?(/.*)', line)
    return unquote(match.group(1)) if match else None


def linux_context():
    """The clipboard is the only portable source on Linux: a link, copied files or plain text.

    Read like the window reads it on macOS and Windows: only a clipboard that is one link is a
    link. A link followed by more lines is text, so nothing after the first line is dropped.
    """
    text = _linux_clipboard().strip()
    if not text:
        return {}
    if re.fullmatch(r'https?://\S+', text):
        return {'url': text}
    # File managers copy files as a text/uri-list: one file:// URI per line, '#' lines are comments.
    lines = [line.strip() for line in text.splitlines()]
    paths = [_file_uri_path(line) for line in lines if line and not line.startswith('#')]
    if paths and all(paths):
        paths = [path for path in paths if Path(path).is_file()]
        return {'dosyalar': paths[:20]} if paths else {}
    return {'metin': text}


def gather_context():
    if sys.platform == 'darwin':
        return mac_context()
    if sys.platform.startswith('linux'):
        return linux_context()
    if os.name == 'nt':
        return windows_context()
    return {}


# ---------------------------------------------------------------- popup

PALETTE = {'bg': '#0A0E17', 'card': '#111827', 'card_line': '#1E293B', 'line': '#172033', 'text': '#F1F5F9',
           'soft': '#CBD5E1', 'muted': '#64748B', 'faint': '#3F4C63', 'accent': '#F5C84C', 'accent_text': '#1A1405',
           'key': '#151D2C', 'key_line': '#26324A', 'ok': '#6EE7A8'}
KIND_LABEL = {'youtube': 'VİDEO', 'x': 'TWEET', 'mail': 'MAİL', 'github': 'REPO', 'pdf': 'PDF', 'makale': 'SAYFA'}


def _font(size, weight='normal'):
    if sys.platform == 'darwin':
        family = '.AppleSystemUIFont'
    elif os.name == 'nt':
        family = 'Segoe UI Variable Text' if sys.getwindowsversion().build >= 22000 else 'Segoe UI'
    else:
        family = 'DejaVu Sans'
    return (family, size, weight)


def _nsstring(objc, send, text):
    import ctypes
    return send(objc.objc_getClass(b'NSString'), b'stringWithUTF8String:', ctypes.c_void_p, ctypes.c_char_p(text.encode('utf-8')))


def _style_mac(focus=True):
    """Spotlight-like panel: no title bar or buttons, dark appearance, floats over every Space."""
    try:
        import ctypes
        objc, send = _objc()
        app = send(objc.objc_getClass(b'NSApplication'), b'sharedApplication')
        send(app, b'setActivationPolicy:', ctypes.c_void_p, ctypes.c_long(1))  # accessory: no Dock icon
        dark = send(objc.objc_getClass(b'NSAppearance'), b'appearanceNamed:', ctypes.c_void_p,
                    ctypes.c_void_p(_nsstring(objc, send, 'NSAppearanceNameDarkAqua')))
        send(app, b'setAppearance:', ctypes.c_void_p, ctypes.c_void_p(dark))
        windows = send(app, b'windows')
        for index in range(send(windows, b'count', ctypes.c_ulong)):
            window = send(windows, b'objectAtIndex:', ctypes.c_void_p, ctypes.c_ulong(index))
            mask = send(window, b'styleMask', ctypes.c_ulong)
            send(window, b'setStyleMask:', ctypes.c_void_p, ctypes.c_ulong(mask | (1 << 15)))  # full-size content
            send(window, b'setTitlebarAppearsTransparent:', ctypes.c_void_p, ctypes.c_bool(True))
            send(window, b'setTitleVisibility:', ctypes.c_void_p, ctypes.c_long(1))
            for button in range(3):
                handle = send(window, b'standardWindowButton:', ctypes.c_void_p, ctypes.c_long(button))
                if handle:
                    send(handle, b'setHidden:', ctypes.c_void_p, ctypes.c_bool(True))
            send(window, b'setMovableByWindowBackground:', ctypes.c_void_p, ctypes.c_bool(True))
            send(window, b'setCollectionBehavior:', ctypes.c_void_p, ctypes.c_ulong(1 | 256))  # all Spaces, over full screen
            send(window, b'setLevel:', ctypes.c_void_p, ctypes.c_long(25))
            send(window, b'setHasShadow:', ctypes.c_void_p, ctypes.c_bool(True))
        if focus:
            send(app, b'activateIgnoringOtherApps:', ctypes.c_void_p, ctypes.c_bool(True))
    except Exception:
        pass


def _round_rect(canvas, x1, y1, x2, y2, radius, **options):
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
              x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


def _fit(canvas, text, font, width):
    """Trim with an ellipsis so the text fits `width` pixels in `font`."""
    import tkinter.font as tkfont
    measure = tkfont.Font(root=canvas, font=font).measure
    if measure(text) <= width:
        return text
    while text and measure(text + '…') > width:
        text = text[:-1]
    return text.rstrip() + '…'


# Clipboard image (#274), Windows only for now. After Win+Shift+S the clipboard holds the picture and a
# file-drop entry for a temp file that Windows deletes later. Tk hands that entry to clipboard_get() as
# text, so the popup saved the path and lost the picture. Pure helpers first, then the reader and the card.

IMAGE_FRESH = 600  # seconds; only a screenshot younger than this starts switched on, the rest waits for Tab
IMAGE_LIMIT = 64 * 1024 * 1024  # bytes taken from the clipboard; two 4K screens side by side fit as a bitmap
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
# The reported path is ...\AppData\Local\Packages\<app>\TempState\ScreenClip\{GUID}.png. Any picture under a
# packaged app's TempState counts, so the folder below it is not relied on.
SNIP_TEMP = re.compile(r'[a-z]:/(?:[^/\r\n]+/)*packages/[^/\r\n]+/tempstate/(?:[^/\r\n]+/)*[^/\r\n]+\.(?:png|jpe?g|bmp|gif)', re.I)


def png_info(data):
    """(width, height, end offset) of a complete PNG at the start of `data`, else None. Every CRC is checked."""
    import zlib
    view = memoryview(data)
    if bytes(view[:8]) != PNG_MAGIC:
        return None
    position, size, pixels = 8, None, False
    while position + 12 <= len(view):
        end = position + 12 + int.from_bytes(view[position:position + 4], 'big')
        kind = bytes(view[position + 4:position + 8])
        if end > len(view) or zlib.crc32(view[position + 4:end - 4]) != int.from_bytes(view[end - 4:end], 'big'):
            return None
        if size is None:
            if kind != b'IHDR' or end - position != 25:
                return None
            size = (int.from_bytes(view[position + 8:position + 12], 'big'), int.from_bytes(view[position + 12:position + 16], 'big'))
            if not (0 < size[0] < 2 ** 31 and 0 < size[1] < 2 ** 31):
                return None
        elif kind == b'IDAT':
            pixels = True
        elif kind == b'IEND':
            return (size[0], size[1], end) if pixels else None
        position = end
    return None


def _png_chunk(kind, body):
    import zlib
    return len(body).to_bytes(4, 'big') + kind + body + zlib.crc32(body, zlib.crc32(kind)).to_bytes(4, 'big')


def dib_to_png(data, level=2):
    """Packed DIB (CF_DIB: header, optional masks and colour table, pixels) -> (PNG bytes, width, height).

    Covers what screens and screenshot tools produce: 24 and 32 bits per pixel, BI_RGB and byte-aligned
    BI_BITFIELDS, bottom-up and top-down rows, padded rows. The PNG is always opaque: the fourth byte of a
    32-bit DIB is unused far more often than it is alpha (all zero after a screen grab), and a source that
    means transparency also offers a real PNG, which is taken first. None for any other layout.
    """
    import struct
    import zlib
    if len(data) < 40:
        return None
    header, width, height, _planes, bits, compression = struct.unpack_from('<IiiHHI', data, 0)
    used = struct.unpack_from('<I', data, 32)[0]
    rows = abs(height)
    if not (40 <= header <= len(data) and 0 < width <= 65535 and 0 < rows <= 65535 and bits in (24, 32) and used <= 256):
        return None
    step, start, stride = bits // 8, header + used * 4, (width * bits + 31) // 32 * 4
    red, green, blue = 2, 1, 0  # BI_RGB pixels are stored blue, green, red
    if compression == 3:  # BI_BITFIELDS: three masks after a 40-byte header, inside a V4/V5 one
        if bits != 32 or 40 < header < 52 or len(data) < 52:
            return None
        masks = struct.unpack_from('<III', data, 40)
        order = [{0xFF: 0, 0xFF00: 1, 0xFF0000: 2, 0xFF000000: 3}.get(mask) for mask in masks]
        if None in order or len(set(order)) != 3:
            return None
        red, green, blue = order
        if header == 40 or (data[header:header + 12] == data[40:52] and len(data) >= start + 12 + stride * rows):
            start += 12  # some writers repeat the masks after a V4/V5 header
    elif compression != 0:
        return None
    if len(data) < start + stride * rows:
        return None
    line = bytearray(1 + width * 3)  # filter type 0, then red, green, blue
    packer = zlib.compressobj(level)
    packed = []
    for index in range(rows):
        at = start + (index if height < 0 else rows - 1 - index) * stride
        row = data[at:at + width * step]
        line[1::3], line[2::3], line[3::3] = row[red::step], row[green::step], row[blue::step]
        packed.append(packer.compress(line))
    packed.append(packer.flush())
    head = struct.pack('>IIBBBBB', width, rows, 8, 2, 0, 0, 0)
    return PNG_MAGIC + _png_chunk(b'IHDR', head) + _png_chunk(b'IDAT', b''.join(packed)) + _png_chunk(b'IEND', b''), width, rows


def snip_path(text):
    """The Snipping Tool's temp picture when `text` is nothing but its path (either slash, optional quotes), else None."""
    text = str(text or '').strip().strip('"')
    return text if SNIP_TEMP.fullmatch(text.replace('\\', '/')) else None


def _win_clipboard_payload(limit=IMAGE_LIMIT, tries=6):
    """Raw image bytes on the Windows clipboard: {'image': bool, 'png': bytes or None, 'dib': bytes or None}.

    None when the clipboard cannot be opened (another process holds it). Handles and pointers are declared
    for 64-bit, each payload is copied out under its own lock, and the clipboard is closed before anything
    is converted or written; a PNG is only checked first, to know whether the bitmap is needed at all.
    CF_DIB is asked for instead of CF_DIBV5: Windows builds it from whichever bitmap format the source
    gave, and its header has no V5 mask-offset ambiguity.
    """
    import ctypes
    import time
    from ctypes import wintypes
    user32, kernel32 = ctypes.WinDLL('user32'), ctypes.WinDLL('kernel32')  # private copies: prototypes stay local
    user32.OpenClipboard.argtypes, user32.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
    user32.CloseClipboard.argtypes, user32.CloseClipboard.restype = [], wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes, user32.IsClipboardFormatAvailable.restype = [wintypes.UINT], wintypes.BOOL
    user32.RegisterClipboardFormatW.argtypes, user32.RegisterClipboardFormatW.restype = [wintypes.LPCWSTR], wintypes.UINT
    user32.GetClipboardData.argtypes, user32.GetClipboardData.restype = [wintypes.UINT], wintypes.HANDLE
    kernel32.GlobalSize.argtypes, kernel32.GlobalSize.restype = [wintypes.HGLOBAL], ctypes.c_size_t
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes, kernel32.GlobalUnlock.restype = [wintypes.HGLOBAL], wintypes.BOOL
    for attempt in range(tries):
        if user32.OpenClipboard(None):
            break
        if attempt + 1 < tries:
            time.sleep(0.02 * (attempt + 1))
    else:
        return None
    found = {'image': False, 'png': None, 'dib': None}

    def take(code):
        if not code or not user32.IsClipboardFormatAvailable(code):
            return None
        found['image'] = True
        handle = user32.GetClipboardData(code)
        size = kernel32.GlobalSize(handle) if handle else 0
        pointer = kernel32.GlobalLock(handle) if 0 < size <= limit else None
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)
    try:
        for name in ('PNG', 'image/png'):  # what the Snipping Tool, browsers and Office put next to the bitmap
            found['png'] = found['png'] or take(user32.RegisterClipboardFormatW(name))
        if not (found['png'] and png_info(found['png'])):
            found['dib'] = take(8)  # CF_DIB
    finally:
        user32.CloseClipboard()
    return found


def _clipboard_payload():
    """Raw image bytes on the system clipboard. Only Windows has a reader; everywhere else nothing changes."""
    return _win_clipboard_payload() if os.name == 'nt' else None


def clipboard_image(clip, context, at=None):
    """The image on the clipboard as a temp PNG the popup shows and can attach, or None to change nothing.

    Offered only when nothing else would be saved: no page, file or text from the context, and the
    clipboard text is empty or just the screenshot's own temp path. Real text or a link stays the capture
    exactly as before. Only a provably fresh screenshot starts switched on: its temp file is still there
    and younger than IMAGE_FRESH. Every other picture is shown and waits for Tab. Failure returns None.
    """
    import time
    folder = None
    try:
        own = snip_path(clip)
        if context.get('url') or context.get('dosyalar') or context.get('metin') or (str(clip or '').strip() and not own):
            return None
        payload = _clipboard_payload()
        if not payload or not payload.get('image'):
            return None
        png = size = None
        info = png_info(payload['png']) if payload.get('png') else None
        if info:
            png, size = bytes(payload['png'][:info[2]]), info[:2]
        elif payload.get('dib'):
            made = dib_to_png(payload['dib'])
            if made:
                png, size = made[0], made[1:]
        if png is None and own and os.path.isfile(own) and os.path.getsize(own) <= IMAGE_LIMIT:
            raw = Path(own).read_bytes()  # a bitmap this reader cannot convert: the screenshot's file is the same picture
            info = png_info(raw)
            if info:
                png, size = raw[:info[2]], info[:2]
        if png is None:
            return None
        at = time.time() if at is None else at
        try:
            age = max(0.0, at - os.path.getmtime(own)) if own else None
        except OSError:
            age = None  # Windows already deleted the temp file: not a screenshot from a moment ago
        fresh = age is not None and age <= IMAGE_FRESH  # an agent reads what is saved: no proof, no attachment
        folder = tempfile.mkdtemp(prefix='beyin-yakala-')
        path = Path(folder) / ('pano-goruntusu-' + datetime.fromtimestamp(at).strftime('%Y%m%d-%H%M%S') + '.png')
        path.write_bytes(png)
        return {'path': str(path), 'folder': folder, 'width': size[0], 'height': size[1], 'age': age, 'stale': bool(own) and not fresh, 'on': fresh}
    except Exception:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        return None


def _ago(seconds):
    minutes = int(seconds // 60)
    if minutes < 1:
        return 'az önce'
    if minutes < 60:
        return str(minutes) + ' dk önce'
    return str(minutes // 60) + ' saat önce' if minutes < 2880 else str(minutes // 1440) + ' gün önce'


def image_lines(image, on):
    """Headline and detail line of the clipboard image card for the current switch state."""
    facts = ['%d×%d' % (image['width'], image['height'])]
    if image['age'] is not None:
        facts.append(_ago(image['age']))
    elif image['stale']:
        facts.append('eski, geçici dosyası silinmiş')
    facts.append('Tab ile çıkar' if on else 'Tab ile ekle')
    return 'Panodaki görüntü' if on else 'Panodaki görüntü eklenmeyecek', ' · '.join(facts)


def _image_thumb(root, image, box=(112, 54)):
    """Small Tk copy of the picture so an old screenshot is recognised at a glance; None when Tk cannot read it."""
    if image['width'] * image['height'] > 24_000_000:
        return None
    try:
        import tkinter as tk
        full = tk.PhotoImage(master=root, file=image['path'])
        factor = max(1, -(-full.width() // box[0]), -(-full.height() // box[1]))
        return full.subsample(factor) if factor > 1 else full
    except Exception:
        return None


def _image_card(canvas, root, image, y, width, pad, changed):
    """Preview card for the clipboard image; Tab or a click switches it. Calls changed(on), returns the next y."""
    import tkinter.font as tkfont
    P, height, state = PALETTE, 78, {'on': bool(image['on'])}
    items = [_round_rect(canvas, pad, y, width - pad, y + height, 14, fill=P['card'], outline=P['card_line'])]
    right = width - pad - 16
    thumb = _image_thumb(root, image)
    if thumb is not None:
        image['thumb'] = thumb  # Tk drops a picture nothing refers to
        items.append(canvas.create_image(right, y + height / 2, image=thumb, anchor='e'))
        right -= thumb.width() + 14
    chip_font = _font(10, 'bold')
    chip_w = tkfont.Font(root=root, font=chip_font).measure('GÖRSEL') + 18
    chip_y, text_x = y + 28, pad + 16 + chip_w + 12
    chip = _round_rect(canvas, pad + 16, chip_y - 11, pad + 16 + chip_w, chip_y + 11, 8, fill=P['accent'], outline='')
    label = canvas.create_text(pad + 16 + chip_w / 2, chip_y, text='GÖRSEL', fill=P['accent_text'], font=chip_font)
    headline = canvas.create_text(text_x, chip_y, anchor='w', fill=P['text'], font=_font(14, 'bold'))
    detail = canvas.create_text(pad + 16, y + height - 20, anchor='w', fill=P['muted'], font=_font(12))

    def draw():
        on = state['on']
        first, second = image_lines(image, on)
        canvas.itemconfigure(chip, fill=P['accent'] if on else P['key'], outline='' if on else P['key_line'])
        canvas.itemconfigure(label, fill=P['accent_text'] if on else P['muted'])
        canvas.itemconfigure(headline, text=_fit(canvas, first, _font(14, 'bold'), right - text_x), fill=P['text'] if on else P['muted'])
        canvas.itemconfigure(detail, text=_fit(canvas, second, _font(12), right - pad - 16))
        changed(on)

    def toggle(_event=None):
        state['on'] = not state['on']
        draw()
        return 'break'
    for item in items + [chip, label, headline, detail]:
        canvas.tag_bind(item, '<Button-1>', toggle)
    root.bind('<Tab>', toggle)
    draw()
    return y + height + 12


def _image_done(image):
    """The vault has its own copy by now (or the user closed the window): drop the temp file."""
    if image:
        image.pop('thumb', None)
        shutil.rmtree(image['folder'], ignore_errors=True)


def repair_mojibake(text):
    """UTF-8 bytes that a tool decoded as Mac Roman or cp1252 ('pahalƒ±' for 'pahalı'); else unchanged."""
    if not text or text.isascii():
        return text
    for codec in ('mac_roman', 'cp1252'):
        try:
            fixed = text.encode(codec).decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != text:
            return fixed
    return text


CLIP_LIMIT = 1_000_000  # bytes taken from a clipboard helper: a card is a note, not a file transfer
CLIP_TIMEOUT = 2  # seconds: the window must not wait on a clipboard owner that never answers
CLIP_CUT = '\n\n[Pano metni 1 MB sınırında kesildi.]'


def _tool_text(command):
    """Text printed by a clipboard helper; '' when it fails, is missing or hangs. Never raises.

    The bytes are decoded as UTF-8 here. With `text=True` a Latin-5 locale garbles Turkish and
    one stray byte raises UnicodeDecodeError, which closes the window before it opens.
    """
    try:
        result = subprocess.run(command, capture_output=True, timeout=CLIP_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ''
    if result.returncode != 0:
        return ''
    text = result.stdout[:CLIP_LIMIT].decode('utf-8', 'replace')
    return text.rstrip('\ufffd') + CLIP_CUT if len(result.stdout) > CLIP_LIMIT else text


def _wl_paste():
    """Wayland clipboard as text; None when this is not a Wayland session or wl-clipboard is missing.

    Tk runs through XWayland and can fail there with "CLIPBOARD selection doesn't exist" (#278).
    `--type text` matters: without it wl-paste hands over whatever is offered, image bytes
    included. Nothing copied, or only an image, is a non-zero exit and reads as ''.
    """
    tool = _which('wl-paste') if os.environ.get('WAYLAND_DISPLAY') else None
    return _tool_text([tool, '--no-newline', '--type', 'text']) if tool else None


def _clipboard(root):
    value = _wl_paste()
    if value:
        return repair_mojibake(value)
    try:
        value = root.clipboard_get()
    except Exception:
        return ''
    return repair_mojibake(value) if isinstance(value, str) else ''


def _hotkey_hint(vault, state=None):
    try:
        label = json.loads(_state_path(state or resolve_state(Path(vault))).read_text(encoding='utf-8')).get('kisayol')
    except (OSError, ValueError, AttributeError):
        label = None
    return (label + ' ile her yerden açılır') if label else 'İkinci beynine kaydedilir'


def popup(vault, context=None, state=None):
    """Command-palette window: what will be saved, one line for why, Enter saves, Esc closes."""
    context = dict(context or {})
    try:
        import tkinter as tk
    except ImportError:
        return _popup_fallback(vault, context, state)
    snapshot = os.environ.get('BEYIN_YAKALA_SNAPSHOT')
    root = tk.Tk()
    root.withdraw()
    clip = _clipboard(root).strip()
    image = clipboard_image(clip, context)  # Windows: a picture on the clipboard (#274); None changes nothing
    if image:
        clip = ''  # at most the screenshot's own temp path, which is not the note
    clip_is_url = bool(re.match(r'^https?://\S+$', clip))
    url = context.get('url') or (clip if clip_is_url else None)
    files = context.get('dosyalar') or []
    clip_text = clip if clip and not clip_is_url else ''
    include = {'on': bool(clip_text) and not url and not files}

    P, W, pad = PALETTE, 640, 26
    root.title('Beyne at')
    root.configure(bg=P['bg'])
    root.resizable(False, False)
    if os.name == 'nt':
        root.overrideredirect(True)
    canvas = tk.Canvas(root, width=W, highlightthickness=0, bd=0, bg=P['bg'])
    canvas.pack(fill='both', expand=True)

    # Header: brand mark, name, where it came from.
    y = 30
    canvas.create_oval(pad, y - 6, pad + 12, y + 6, fill=P['accent'], outline='')
    canvas.create_text(pad + 22, y, text='Beyne at', anchor='w', fill=P['text'], font=_font(13, 'bold'))
    source = context.get('uygulama') or ''
    if source:
        canvas.create_text(W - pad, y, text=_fit(canvas, source, _font(12), 260), anchor='e', fill=P['muted'], font=_font(12))

    # Main field: large, borderless, with a placeholder.
    y = 64
    entry = tk.Entry(root, font=_font(21), fg=P['text'], bg=P['bg'], insertbackground=P['accent'], insertwidth=2,
                     relief='flat', bd=0, highlightthickness=0)
    canvas.create_window(pad, y, window=entry, anchor='nw', width=W - 2 * pad, height=40)
    placeholder_text = 'Neden kaydediyorsun?' if (url or files or clip_text) else 'Aklındakini yaz…'
    hint = {'on': False}

    def show_hint():
        entry.delete(0, 'end')
        entry.insert(0, placeholder_text)
        entry.configure(fg=P['faint'])
        entry.icursor(0)
        hint['on'] = True

    def on_key(event):
        # Embedded windows sit above canvas items, so the hint lives inside the field itself.
        if hint['on'] and (event.char and event.char.isprintable() or event.keysym in ('BackSpace', 'Delete')):
            entry.delete(0, 'end')
            entry.configure(fg=P['text'])
            hint['on'] = False
            if event.keysym in ('BackSpace', 'Delete'):
                return 'break'
        if hint['on'] and event.keysym in ('Left', 'Right', 'Home', 'End'):
            return 'break'

    def after_key(_event=None):
        if not hint['on'] and not entry.get():
            show_hint()
    def on_paste(_event=None):
        if hint['on']:
            entry.delete(0, 'end')
            entry.configure(fg=P['text'])
            hint['on'] = False
    entry.bind('<KeyPress>', on_key)
    entry.bind('<<Paste>>', on_paste, add='+')
    entry.bind('<KeyRelease>', after_key)
    entry.bind('<Button-1>', lambda _e: (entry.focus_set(), entry.icursor(0), 'break')[-1] if hint['on'] else None)
    show_hint()
    y += 56
    canvas.create_line(pad, y, W - pad, y, fill=P['line'])
    y += 16

    # Preview card.
    if files or url or clip_text:
        if files:
            kind = 'DOSYA'
            headline = Path(files[0]).name + ('  +' + str(len(files) - 1) if len(files) > 1 else '')
            detail = str(Path(files[0]).parent)
        elif url:
            kind = KIND_LABEL.get(kind_of(url), 'SAYFA')
            headline, detail = context.get('baslik') or url, url
        else:
            kind, headline, detail = 'METİN', _first_line(clip_text, 200), ''
        card_h = 78 if detail and detail != headline else 56
        _round_rect(canvas, pad, y, W - pad, y + card_h, 14, fill=P['card'], outline=P['card_line'])
        chip_font = _font(10, 'bold')
        import tkinter.font as tkfont
        chip_w = tkfont.Font(root=root, font=chip_font).measure(kind) + 18
        chip_y = y + (20 if card_h > 56 else card_h / 2) + (8 if card_h > 56 else 0)
        _round_rect(canvas, pad + 16, chip_y - 11, pad + 16 + chip_w, chip_y + 11, 8, fill=P['accent'], outline='')
        canvas.create_text(pad + 16 + chip_w / 2, chip_y, text=kind, fill=P['accent_text'], font=chip_font)
        text_x = pad + 16 + chip_w + 12
        canvas.create_text(text_x, chip_y, text=_fit(canvas, headline, _font(14, 'bold'), W - pad - 16 - text_x),
                           anchor='w', fill=P['text'], font=_font(14, 'bold'))
        if card_h > 56:
            canvas.create_text(pad + 16, y + card_h - 20, text=_fit(canvas, detail, _font(12), W - 2 * pad - 32),
                               anchor='w', fill=P['muted'], font=_font(12))
        y += card_h + 12

    # Optional clipboard toggle when something better is already being saved.
    if clip_text and (url or files):
        box = _round_rect(canvas, pad + 2, y + 2, pad + 18, y + 18, 5, fill=P['bg'], outline=P['faint'])
        tick = canvas.create_text(pad + 10, y + 10, text='✓', fill=P['accent_text'], font=_font(10, 'bold'), state='hidden')
        label = canvas.create_text(pad + 28, y + 10, anchor='w', fill=P['soft'], font=_font(12),
                                   text=_fit(canvas, 'Panodaki metni de ekle · ' + _first_line(clip_text, 120), _font(12), W - 2 * pad - 40))

        def toggle(_event=None):
            include['on'] = not include['on']
            canvas.itemconfigure(box, fill=P['accent'] if include['on'] else P['bg'],
                                 outline=P['accent'] if include['on'] else P['faint'])
            canvas.itemconfigure(tick, state='normal' if include['on'] else 'hidden')
            return 'break'
        for item in (box, tick, label):
            canvas.tag_bind(item, '<Button-1>', toggle)
        root.bind('<Tab>', toggle)
        y += 30

    # Clipboard image (#274): shown before it is attached; Tab or a click takes it off or puts it back.
    if image:
        def image_changed(on):
            nonlocal placeholder_text
            files[:] = [image['path']] if on else []
            placeholder_text = 'Neden kaydediyorsun?' if on else 'Aklındakini yaz…'
            if hint['on']:
                show_hint()
        y = _image_card(canvas, root, image, y, W, pad, image_changed)

    # Footer: status on the left, key hints on the right.
    y += 8
    footer_y = y + 14
    status = canvas.create_text(pad, footer_y, anchor='w', fill=P['muted'], font=_font(12),
                                text='Tab: panoyu ekle' if clip_text and (url or files) else _hotkey_hint(vault, state))
    right = W - pad

    def keycap(x_right, key, label):
        import tkinter.font as tkfont
        label_w = tkfont.Font(root=root, font=_font(12)).measure(label)
        canvas.create_text(x_right, footer_y, text=label, anchor='e', fill=P['soft'], font=_font(12))
        key_w = tkfont.Font(root=root, font=_font(11, 'bold')).measure(key) + 14
        x2 = x_right - label_w - 8
        _round_rect(canvas, x2 - key_w, footer_y - 11, x2, footer_y + 11, 6, fill=P['key'], outline=P['key_line'])
        canvas.create_text(x2 - key_w / 2, footer_y, text=key, fill=P['soft'], font=_font(11, 'bold'))
        return x2 - key_w - 18
    right = keycap(right, '↵', 'Kaydet')
    keycap(right, 'esc', 'Kapat')
    height = footer_y + 28
    canvas.configure(height=height)
    result = {}

    def close():
        def fade(alpha):
            if alpha <= 0:
                root.destroy()
                return
            try:
                root.attributes('-alpha', alpha)
            except tk.TclError:
                root.destroy()
                return
            root.after(16, fade, round(alpha - 0.12, 2))
        fade(1.0)

    def save(_event=None):
        text = clip_text if include['on'] else None
        why = '' if hint['on'] else entry.get().strip()
        if not (url or files or text or why):
            canvas.itemconfigure(status, text='Önce bir şey yaz', fill=P['accent'])
            return 'break'
        if not (url or files or text):
            text, why = why, ''  # only a typed line: the line itself is the note
        try:
            result.update(capture(vault, url=url, text=text, files=files, why=why, app=context.get('uygulama'),
                                  title=context.get('baslik') if url else None, state=state))
        except Exception as exc:
            canvas.itemconfigure(status, text=_fit(canvas, 'Kaydedilemedi: ' + str(exc), _font(12), 300), fill=P['accent'])
            return 'break'
        entry.configure(state='disabled', disabledbackground=P['bg'], disabledforeground=P['muted'])
        canvas.itemconfigure(status, text='✓  Beyne atıldı', fill=P['ok'], font=_font(12, 'bold'))
        root.after(520, close)
        return 'break'

    root.bind('<Return>', save)
    root.bind('<KP_Enter>', save)
    root.bind('<Escape>', lambda _e: close())

    root.update_idletasks()
    x = (root.winfo_screenwidth() - W) // 2
    top = max(80, int(root.winfo_screenheight() * 0.22))
    root.geometry('%dx%d+%d+%d' % (W, height, x, top))
    try:
        root.attributes('-alpha', 0.0)
        root.attributes('-topmost', True)
    except tk.TclError:
        pass
    root.deiconify()
    if sys.platform == 'darwin':
        _style_mac(focus=not snapshot)
    if not snapshot:
        root.lift()
        root.focus_force()
        entry.focus_set()

    def fade_in(alpha=0.0):
        alpha = min(1.0, alpha + 0.2)
        try:
            root.attributes('-alpha', alpha)
        except tk.TclError:
            return
        if alpha < 1.0:
            root.after(14, fade_in, alpha)
    fade_in()
    if snapshot and sys.platform == 'darwin':
        # Development aid: capture only this window's rectangle without taking focus, then close.
        def shoot():
            region = '%d,%d,%d,%d' % (root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height())
            subprocess.run(['screencapture', '-x', '-o', '-R', region, snapshot], capture_output=True)
            root.destroy()
        root.after(700, shoot)
    root.mainloop()
    _image_done(image)
    return result or {'status': 'vazgecildi'}


LINUX_NO_WINDOW = ('Pencere acilamiyor: bu Python\'da tkinter yok, kdialog ya da zenity de kurulu degil. Birini kur '
                   '(Arch: sudo pacman -S tk, Debian/Ubuntu: sudo apt install python3-tk) ya da beyin.py yakala ekle kullan.')


def _linux_window():
    """What can show the window here: 'tkinter', the path of kdialog or zenity, or None."""
    try:
        import tkinter  # noqa: F401
        return 'tkinter'
    except ImportError:
        return _which('kdialog', 'zenity')


def _linux_ask(shown=''):
    """Reason typed in kdialog or zenity; None when cancelled. Neither tool: an error, nothing is saved unseen.

    `shown` comes from the clipboard. It is never an argument of its own and never the start of one:
    it follows the fixed question inside the value of --inputbox / --text. That fixed first line also
    keeps Qt from reading the label as rich text. Both tools expand backslash escapes, and zenity
    drops a single underscore as a mnemonic (--entry has no Pango markup), hence the doubling.
    """
    shown = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(shown)).strip()[:120].replace('\\', '\\\\')
    kdialog, zenity = _which('kdialog'), _which('zenity')
    if kdialog:
        command = [kdialog, '--title', 'Beyne at', '--inputbox', 'Neden kaydediyorsun?' + ('\n' + shown if shown else ''), '']
    elif zenity:
        command = [zenity, '--entry', '--title', 'Beyne at', '--text',
                   'Neden kaydediyorsun?' + ('\n' + shown.replace('_', '__') if shown else '')]
    else:
        raise ValueError(LINUX_NO_WINDOW)
    code, out = _try(command, timeout=300)
    return out.strip() if code == 0 else None


def _popup_fallback(vault, context, state=None):
    """No tkinter (some Homebrew/Linux Pythons): the OS dialog asks only for the reason."""
    why = ''
    url, files, text = context.get('url'), context.get('dosyalar') or [], context.get('metin') or ''
    if sys.platform == 'darwin':
        why = _osascript('text returned of (display dialog "Neden kaydediyorsun?" default answer "" with title "Beyne at")', timeout=300) or ''
    elif sys.platform.startswith('linux'):
        why = _linux_ask(url or ', '.join(Path(f).name for f in files) or text)
        if why is None:
            return {'status': 'vazgecildi'}
    if not (url or files or text or why):
        return {'status': 'vazgecildi'}
    if text and not (url or files):  # clipboard text is the content; the dialog answer is the reason
        return capture(vault, text=text, why=why, app=context.get('uygulama'), title=context.get('baslik'), state=state)
    return capture(vault, url=url, text=None if url or files else why, files=files,
                   why=why if url or files else '', app=context.get('uygulama'), title=context.get('baslik'), state=state)


# ---------------------------------------------------------------- hotkey spec

DEFAULT_HOTKEY = 'ctrl+alt+b'
MODIFIERS = {'cmd': 'cmd', 'command': 'cmd', '⌘': 'cmd', 'super': 'cmd', 'win': 'cmd',
             'ctrl': 'ctrl', 'control': 'ctrl', 'ctl': 'ctrl', '⌃': 'ctrl',
             'alt': 'alt', 'opt': 'alt', 'option': 'alt', '⌥': 'alt',
             'shift': 'shift', '⇧': 'shift'}
MAC_BITS = {'cmd': 0x100, 'shift': 0x200, 'alt': 0x800, 'ctrl': 0x1000}
MAC_SYMBOL = {'ctrl': '⌃', 'alt': '⌥', 'shift': '⇧', 'cmd': '⌘'}
MAC_NAMED = {'space': 49, 'bosluk': 49, 'return': 36, 'enter': 36, 'tab': 48, 'esc': 53, 'escape': 53,
             'f1': 122, 'f2': 120, 'f3': 99, 'f4': 118, 'f5': 96, 'f6': 97, 'f7': 98, 'f8': 100,
             'f9': 101, 'f10': 109, 'f11': 103, 'f12': 111}
# ANSI positions, used only when the live keyboard layout cannot be read.
MAC_ANSI = dict(zip('asdfhgzxcv bqweryt123465=97-80]ou[ip lj\'k;\\,/nm.', range(50)))
MAC_ANSI.pop(' ', None)


def parse_hotkey(spec):
    """'cmd+"' / 'ctrl+alt+b' / '⌘⇧K' -> (sorted modifiers, key). The key is the last part."""
    text = str(spec or '').strip()
    if not text:
        raise ValueError('Kisayol bos olamaz; ornek: ctrl+alt+b ya da cmd+"')
    for symbol in '⌘⌃⌥⇧':
        text = text.replace(symbol, MAC_SYMBOL_REVERSE[symbol] + '+')
    parts = text.split('+')
    if text.endswith('++'):  # the key itself is '+'
        parts = parts[:-2] + ['+']
    key = parts[-1].strip()
    mods = []
    for part in parts[:-1]:
        name = MODIFIERS.get(part.strip().lower())
        if not name:
            raise ValueError('Bilinmeyen tus: ' + part.strip() + ' (cmd, ctrl, alt/option, shift)')
        if name not in mods:
            mods.append(name)
    if not key:
        raise ValueError('Kisayolda tus eksik; ornek: cmd+"')
    if len(key) > 1:
        key = key.lower()
        if key not in MAC_NAMED:
            raise ValueError('Bilinmeyen tus adi: ' + key)
    if not mods and not re.fullmatch(r'f\d{1,2}', key):
        raise ValueError('En az bir degistirici tus gerekir (cmd, ctrl, alt, shift)')
    order = ['ctrl', 'alt', 'shift', 'cmd']
    return sorted(mods, key=order.index), key


KEYPAD = {65, 67, 69, 71, 75, 76, 78, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92}
MAC_SYMBOL_REVERSE = {'⌘': 'cmd', '⌃': 'ctrl', '⌥': 'alt', '⇧': 'shift'}


def _mac_layout_keycode(char):
    """Physical key that types `char` on the active layout (Turkish Q puts '"' left of 1)."""
    import ctypes
    from ctypes import POINTER, byref, c_uint8, c_uint16, c_uint32, c_ulong, c_void_p
    carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = c_void_p
    carbon.TISGetInputSourceProperty.restype = c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [c_void_p, c_void_p]
    cf.CFDataGetBytePtr.restype = c_void_p
    cf.CFDataGetBytePtr.argtypes = [c_void_p]
    carbon.LMGetKbdType.restype = c_uint8
    carbon.UCKeyTranslate.argtypes = [c_void_p, c_uint16, c_uint16, c_uint32, c_uint32, c_uint32,
                                      POINTER(c_uint32), c_ulong, POINTER(c_ulong), POINTER(c_uint16)]
    source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
    data = carbon.TISGetInputSourceProperty(source, c_void_p.in_dll(carbon, 'kTISPropertyUnicodeKeyLayoutData'))
    if not data:
        return None
    layout, kind = cf.CFDataGetBytePtr(data), carbon.LMGetKbdType()
    for shift in (0, 2):  # unshifted first; 2 = shiftKey >> 8
        for code in (c for c in range(128) if c not in KEYPAD):
            dead, length, buffer = c_uint32(0), c_ulong(0), (c_uint16 * 4)()
            carbon.UCKeyTranslate(layout, code, 3, shift, kind, 1, byref(dead), 4, byref(length), buffer)
            typed = ''.join(chr(buffer[i]) for i in range(length.value))
            if typed == char or (len(char) == 1 and typed.lower() == char.lower() and char.isalpha() and not shift):
                return code, bool(shift)
    return None


def mac_hotkey(spec):
    """-> (keycode, carbon modifier bits, label such as ⌘\")."""
    mods, key = parse_hotkey(spec)
    shifted = False
    if key in MAC_NAMED:
        code = MAC_NAMED[key]
    else:
        found = None
        try:
            found = _mac_layout_keycode(key)
        except (OSError, AttributeError, ValueError):
            found = None
        if found is None and key.lower() in MAC_ANSI:
            found = (MAC_ANSI[key.lower()], False)
        if found is None:
            raise ValueError('Bu karakter etkin klavye duzeninde bir tusa karsilik gelmiyor: ' + key)
        code, shifted = found
    if shifted and 'shift' not in mods:
        mods.append('shift')  # the character itself needs Shift on this layout
    bits = sum(MAC_BITS[m] for m in mods)
    label = ''.join(MAC_SYMBOL[m] for m in ('ctrl', 'alt', 'shift', 'cmd') if m in mods) + \
        (key.upper() if len(key) == 1 else key.capitalize())
    return code, bits, label


def windows_hotkey(spec):
    """Start-menu shortcut keys accept Ctrl/Alt/Shift with a letter, digit or F-key; no Windows key."""
    mods, key = parse_hotkey(spec)
    if 'cmd' in mods:
        raise ValueError('Windows kisayolunda Win/Cmd tusu kullanilamaz; ctrl, alt ve shift kullan')
    if not re.fullmatch(r'[a-z0-9]|f\d{1,2}', key.lower()):
        raise ValueError('Windows kisayolunda yalniz harf, rakam ya da F tusu olabilir')
    if len([m for m in mods if m in ('ctrl', 'alt')]) == 0:
        raise ValueError('Windows kisayolu Ctrl ya da Alt icermeli')
    names = {'ctrl': 'CTRL', 'alt': 'ALT', 'shift': 'SHIFT'}
    value = '+'.join([names[m] for m in mods] + [key.upper()])
    return value, '+'.join([m.capitalize() for m in mods] + [key.upper()])


QT_MODS = {'ctrl': 0x04000000, 'alt': 0x08000000, 'shift': 0x02000000, 'cmd': 0x10000000}
KDE_MODS = {'ctrl': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift', 'cmd': 'Meta'}
GNOME_MODS = {'ctrl': '<Control>', 'alt': '<Alt>', 'shift': '<Shift>', 'cmd': '<Super>'}


def linux_hotkey(spec):
    """-> (KDE text, Qt key int, GNOME accelerator, label); letter, digit, F1-F12 or space."""
    mods, key = parse_hotkey(spec)
    key = {'bosluk': 'space'}.get(key.lower(), key)
    if not (re.fullmatch(r'[A-Za-z0-9]', key) or key.lower() == 'space' or re.fullmatch(r'[fF]([1-9]|1[0-2])', key)):
        raise ValueError('Linux kisayolunda yalniz harf, rakam, bosluk ya da F1-F12 tusu olabilir')
    if not [m for m in mods if m in ('ctrl', 'alt', 'cmd')]:
        raise ValueError('Linux kisayolu Ctrl, Alt ya da Super icermeli')
    if key.lower() == 'space':
        name, code, accel = 'Space', 0x20, 'space'
    elif len(key) == 1:
        name, code, accel = key.upper(), ord(key.upper()), key.lower()
    else:
        name, code, accel = key.upper(), 0x01000030 + int(key[1:]) - 1, key.upper()
    qt = sum(QT_MODS[m] for m in mods) + code
    kde = '+'.join([KDE_MODS[m] for m in mods] + [name])
    return kde, qt, ''.join(GNOME_MODS[m] for m in mods) + accel, kde.replace('Meta', 'Super')


def saved_hotkey(state):
    try:
        return json.loads(_state_path(state).read_text(encoding='utf-8')).get('tus') or DEFAULT_HOTKEY
    except (OSError, ValueError, AttributeError):
        return DEFAULT_HOTKEY


# ---------------------------------------------------------------- hotkey listeners (macOS & Windows)

def listen_mac(vault, script, keycode=11, modifiers=0x1000 | 0x800):
    """Carbon RegisterEventHotKey: global, no Accessibility permission, standard library only."""
    import ctypes
    from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_int32, c_uint32, c_void_p
    try:
        objc, send = _objc()
        app = send(objc.objc_getClass(b'NSApplication'), b'sharedApplication')
        send(app, b'setActivationPolicy:', ctypes.c_void_p, ctypes.c_long(2))  # prohibited: background daemon, no Dock icon
    except Exception:
        pass
    carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')

    class HotKeyID(Structure):
        _fields_ = [('signature', c_uint32), ('id', c_uint32)]

    class EventTypeSpec(Structure):
        _fields_ = [('eventClass', c_uint32), ('eventKind', c_uint32)]

    handler_type = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)
    carbon.GetApplicationEventTarget.restype = c_void_p
    carbon.InstallEventHandler.argtypes = [c_void_p, handler_type, c_uint32, POINTER(EventTypeSpec), c_void_p, POINTER(c_void_p)]
    carbon.RegisterEventHotKey.argtypes = [c_uint32, c_uint32, HotKeyID, c_void_p, c_uint32, POINTER(c_void_p)]

    def fourcc(text):
        return int.from_bytes(text.encode('ascii'), 'big')

    def pressed(_call, _event, _data):
        try:
            context = mac_context()
            # Prefer the vault copy so an update reaches the window without reinstalling the listener.
            current = Path(vault) / '.claude/scripts' / Path(script).name
            subprocess.Popen([sys.executable, str(current if current.is_file() else script), 'pencere', '--vault', str(vault), '--baglam', json.dumps(context)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception:
            pass
        return 0

    callback = handler_type(pressed)
    target = carbon.GetApplicationEventTarget()
    spec = EventTypeSpec(fourcc('keyb'), 5)  # kEventClassKeyboard / kEventHotKeyPressed
    handler_ref, hotkey_ref = c_void_p(), c_void_p()
    if carbon.InstallEventHandler(target, callback, 1, byref(spec), None, byref(handler_ref)):
        raise SystemExit('Kisayol dinleyicisi kurulamadi')
    # Default keycode 11 = B with controlKey 0x1000 | optionKey 0x800; `kur --tus` passes others.
    if carbon.RegisterEventHotKey(keycode, modifiers, HotKeyID(fourcc('BYKL'), 1), target, 0, byref(hotkey_ref)):
        raise SystemExit('Kisayol baska bir uygulamada kayitli; beyin.py yakala kisayol ile degistir')
    carbon.RunApplicationEventLoop()


def _windows_hotkey_vk(spec):
    mods, key = parse_hotkey(spec)
    if 'cmd' in mods:
        raise ValueError('Windows kisayolunda Win/Cmd tusu kullanilamaz; ctrl, alt ve shift kullan')
    fs_modifiers = 0x4000  # MOD_NOREPEAT
    if 'alt' in mods:
        fs_modifiers |= 0x0001
    if 'ctrl' in mods:
        fs_modifiers |= 0x0002
    if 'shift' in mods:
        fs_modifiers |= 0x0004
    key_lower = key.lower()
    if re.fullmatch(r'f\d{1,2}', key_lower):
        f_num = int(key_lower[1:])
        if not (1 <= f_num <= 24):
            raise ValueError('Gecersiz F tusu: ' + key)
        vk = 0x70 + (f_num - 1)
    elif re.fullmatch(r'[a-z0-9]', key_lower):
        # ASCII only, as in windows_hotkey(): isalnum() also accepts 'ş', whose code point is no virtual key.
        vk = ord(key.upper())
    else:
        raise ValueError('Windows kisayolunda yalniz harf, rakam ya da F tusu olabilir: ' + key)
    return fs_modifiers, vk


WIN_LISTENER = 'Beyne At Dinleyici'
_WIN32 = None


def _win32():
    """(user32, kernel32) with declared prototypes.

    A HANDLE is pointer-sized and the default int return type cuts it to 32 bits. The error
    of a call is read with ctypes.get_last_error(): a later GetLastError() through ctypes may
    already see another call's value. Private instances leave ctypes.windll as other code uses it.
    """
    global _WIN32
    if _WIN32 is None:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        name = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        for function, result, arguments in (
                (kernel32.CreateMutexW, wintypes.HANDLE, [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]),
                (kernel32.CreateEventW, wintypes.HANDLE, [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]),
                (kernel32.OpenMutexW, wintypes.HANDLE, name), (kernel32.OpenEventW, wintypes.HANDLE, name),
                (kernel32.SetEvent, wintypes.BOOL, [wintypes.HANDLE]),
                (kernel32.CloseHandle, wintypes.BOOL, [wintypes.HANDLE]),
                (user32.RegisterHotKey, wintypes.BOOL, [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]),
                (user32.UnregisterHotKey, wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
                (user32.MsgWaitForMultipleObjects, wintypes.DWORD,
                 [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD, wintypes.DWORD]),
                (user32.PeekMessageW, wintypes.BOOL,
                 [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]),
                (user32.AllowSetForegroundWindow, wintypes.BOOL, [wintypes.DWORD])):
            function.restype, function.argtypes = result, arguments
        _WIN32 = (user32, kernel32)
    return _WIN32


def _windows_digest(path):
    import hashlib
    return hashlib.sha256(str(Path(path).resolve()).encode('utf-8')).hexdigest()[:16]


def _windows_mutex(vault):
    """Held by this vault's listener for as long as its key is registered. Local\\ is this logon
    session, where the hotkey lives too."""
    return 'Local\\AvenoxBeyinYakala_' + _windows_digest(vault)


def _windows_stop_event():
    """Shared by every vault: one listener per logon session and Startup folder, the one that
    folder starts. A run with its own APPDATA (the tests) cannot reach the user's listener."""
    return 'Local\\AvenoxBeyinYakalaDur_' + _windows_digest(_windows_dirs()[2])


def _listener_log(state, line):
    """The listener has no console: why it left is kept next to its copy (macOS: launchd stderr)."""
    try:
        folder = Path(state) / 'yakala'
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / 'dinleyici.log', 'a', encoding='utf-8') as out:
            out.write(now().isoformat(timespec='seconds') + ' ' + line + '\n')
    except OSError:
        pass


def listen_windows(vault, script, spec=None, state=None):
    """Win32 RegisterHotKey: global, no external dependencies, standard library ctypes only.

    One listener per logon session, like the single macOS LaunchAgent. Returns 0 when it is
    asked to stop or when this vault's listener already runs; exits 1 when the combination
    cannot be registered. Nothing restarts the process, so a taken key is not retried in a loop.
    """
    import ctypes
    from ctypes import wintypes

    vault_path = Path(vault).resolve()
    state = Path(state) if state else resolve_state(vault_path)
    spec = spec or saved_hotkey(state)
    modifiers, vk = _windows_hotkey_vk(spec)
    user32, kernel32 = _win32()
    hotkey_id = 1

    def leave(reason):
        _listener_log(state, reason)
        raise SystemExit(reason)

    def pressed():
        try:
            context = windows_context()
            # Prefer the vault copy so an update reaches the window without reinstalling the listener.
            current = vault_path / '.claude/scripts' / Path(script).name
            target = current if current.is_file() else Path(script).resolve()
            pythonw = Path(sys.executable).with_name('pythonw.exe')
            exe = str(pythonw if pythonw.is_file() else sys.executable)
            child = subprocess.Popen([exe, str(target), 'pencere', '--vault', str(vault_path),
                                      '--baglam', json.dumps(context)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            # The key press gave this process the right to take the foreground; pass it on, or the
            # window can open behind the active application and the typing goes to that one.
            user32.AllowSetForegroundWindow(child.pid)
        except Exception:
            pass

    # The stop event is also the session lock: the process that created it is the listener.
    ctypes.set_last_error(0)
    stop = kernel32.CreateEventW(None, True, False, _windows_stop_event())  # manual reset, not signalled
    error = ctypes.get_last_error()
    if not stop:
        leave('Kisayol dinleyicisi baslatilamadi (Windows hata %d)' % error)
    mutex = None
    try:
        if error == 183:  # ERROR_ALREADY_EXISTS
            if _windows_listener_running(vault_path):
                return 0
            leave('Baska bir vault icin kisayol dinleyicisi calisiyor; bu vault\'ta beyin.py yakala kur calistir')
        if not user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
            leave('Kisayol kaydedilemedi: %s (Windows hata %d). Baska bir uygulamada kayitli olabilir; '
                  'beyin.py yakala kisayol ile degistir' % (spec, ctypes.get_last_error()))
        try:
            # Created only now, so `durum` and `kur` never see a listener whose key was refused.
            mutex = kernel32.CreateMutexW(None, False, _windows_mutex(vault_path))
            handles = (wintypes.HANDLE * 1)(stop)
            msg = wintypes.MSG()
            while True:
                # 0: the stop event. 1 (the handle count): a message waits. Anything else is an
                # error, and GetMessageW's -1 taught the lesson: leave, never spin on it.
                woke = user32.MsgWaitForMultipleObjects(1, handles, False, 0xFFFFFFFF, 0x04FF)  # INFINITE, QS_ALLINPUT
                if woke == 0:
                    return 0
                if woke != 1:
                    leave('Kisayol dinleyicisi beklerken hata aldi (Windows hata %d)' % ctypes.get_last_error())
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                    if msg.message == 0x0012:  # WM_QUIT
                        return 0
                    if msg.message == 0x0312:  # WM_HOTKEY
                        pressed()
        finally:
            user32.UnregisterHotKey(None, hotkey_id)
    finally:
        if mutex:
            kernel32.CloseHandle(mutex)
        kernel32.CloseHandle(stop)


# ---------------------------------------------------------------- install / remove

TEMPLATE_NAME = 'Beyne at'
SKILL_MARK = '<!-- beyin-yakala: `beyin.py yakala kur` yazdi; kaldir komutu siler -->'
# Written by `kur`, not shipped in the package: released updaters accept only the starter skill paths.
SKILL_MD = '''---
name: beyin-yakala
description: Kullanıcının yakaladığı kaynakları (YouTube videosu, tweet, makale, mail, PDF, dosya, not) ikinci beyne işler; metni çıkarır, dersleri knowledge/ notlarına bağlar. "yakalananları işle", "beyne attıklarımı işle", "inbox'taki kaynaklar", "şu videoyu beyne at" denince ya da oturum başında "Yakalanan N kaynak bekliyor" görüp kullanıcı isteyince kullan.
---
''' + SKILL_MARK + '''

# Beyne at: yakalanan kaynakları işle

Kullanıcı kaynakları tek tuşla yakalar: kısayol (Mac'te Control+Option+B, Windows'ta Ctrl+Alt+B, Linux'ta KDE ve GNOME'da Ctrl+Alt+B),
tarayıcıda Obsidian Web Clipper'ın "Beyne at" şablonu ya da Windows'ta sağ tık > Gönder > Beyne At.
Her yakalama `📥 000-Inbox/Yakala/` içinde tek bir karttır. Kartın `## Neden` bölümü kullanıcının
niyetidir; dersleri o niyete göre seç.

Windows'ta komutlardaki `python3` yerine `py -3` yaz.

## Akış

1. **Metni çıkar.** `python3 beyin.py yakala isle --json`. Ağ kullanır: önce zaten yakalanmış
   içerik, sonra Defuddle (`npx`), YouTube'da altyazı (`yt-dlp`), altyazı yoksa yalnız ses indirilip
   yerel Whisper ile yazıya dökülür ve ses silinir. Kullanıcı ses indirmeyi istemezse `--ses-yok`.
2. **Her `cikarildi` kartı oku.** Kartı ve `ham` dosyasını (`.ham/<id>.md`) oku. Uzun transkriptte
   önce bölüm başlıklarına bak, sonra nedenle ilgili kısmı oku. `ajan-okusun` yönteminde dosyayı
   kendin aç. Mail kartında Gmail/Outlook bağlantın varsa thread'in tamamını oradan oku.
3. **Dersleri bağla.** Kaynak başına 1-5 somut ders çıkar. Önce `knowledge/index.md` ve ilgili
   kavramı oku; mevcut notu geliştir, yoksa `beyin` skill'indeki kuralla `knowledge/concepts/<konu>.md`
   oluştur. Her dersin altında kaynak satırı: kart bağlantısı, URL ve videoda zaman damgası
   (`[12:40]`). Ham metni bilgi notuna kopyalama; kısa alıntı en fazla bir cümle.
4. **Kapat.** `python3 beyin.py yakala bitti <id> --bilgi knowledge/concepts/<konu>.md --ozet "<tek cümle>"`.
   Kalıcı ders yoksa `--ozet "kalıcı ders yok"` yeter; zorla not üretme.
5. **Kullanıcıya söyle.** Kaynak başına bir satır: ne öğrendik, hangi nota gitti.

## Kurallar

- Kullanıcı istemeden kuyruğu işlemeye başlama; oturum bildirimi yalnız bilgi.
- `hata` kartında karttaki `hata` notunu oku ve kullanıcıya ne gerektiğini söyle (sayfa giriş
  istiyorsa Web Clipper ile sayfa açıkken yakalamak, video için `yt-dlp`). Yeniden denemek için
  `python3 beyin.py yakala isle --tekrar`.
- `visibility: private` kartlardan (mail, pano metni, dosya) çıkan derslerde kişi adı, adres,
  numara gibi kişisel veriyi bilgi notuna taşıma.
- Kartları ve `.ham/` dosyalarını silme; kullanıcının arşividir.
- Kurulum ya da kısayol sorunu: `python3 beyin.py yakala durum`. Kurulum `python3 beyin.py yakala kur`,
  kaldırma `python3 beyin.py yakala kaldir` (notlara dokunmaz).
'''


def clipper_template(inbox_path=None):
    return {
        'schemaVersion': '0.1.0',
        'name': TEMPLATE_NAME,
        'behavior': 'create',
        'noteNameFormat': '{{date|date:"YYYY-MM-DD-HHmm"}}-{{title|safe_name|lower|slice:0,48}}',
        'path': inbox_path or INBOX,
        'noteContentFormat': '# {{title}}\n\n## Neden\n\n\n\n## Kaynak\n\n{{url}}\n\n## Secim\n\n{{selection}}\n\n## Icerik\n\n{{content}}\n',
        'properties': [
            {'name': 'tur', 'value': 'yakala', 'type': 'text'},
            {'name': 'durum', 'value': 'bekliyor', 'type': 'text'},
            {'name': 'kaynak_turu', 'value': '', 'type': 'text'},
            {'name': 'baslik', 'value': '{{title}}', 'type': 'text'},
            {'name': 'url', 'value': '{{url}}', 'type': 'text'},
            {'name': 'yakalandi', 'value': '{{date|date:"YYYY-MM-DDTHH:mm:ssZ"}}', 'type': 'text'},
            {'name': 'arac', 'value': 'web-clipper', 'type': 'text'},
            {'name': 'visibility', 'value': 'internal', 'type': 'text'},
        ],
        'triggers': [],
    }


def _links_skills(skills):
    try:
        return any(entry.is_symlink() for entry in Path(skills).iterdir())
    except OSError:
        return False


def _state_path(state):
    return Path(state) / STATE_FILE


def _launch_agent():
    return Path.home() / 'Library/LaunchAgents' / (LAUNCH_LABEL + '.plist')


def _listener_ok(wait=1.2):
    """The listener exits at once when another app already owns the combination.

    `launchctl print` also succeeds for a job that is loaded but stopped, so only
    `state = running` counts.
    """
    import time
    time.sleep(wait)
    probe = subprocess.run(['launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + LAUNCH_LABEL], capture_output=True)
    return probe.returncode == 0 and b'state = running' in probe.stdout


def _agent_vault():
    """Vault the installed LaunchAgent serves, or None when this home has none."""
    import plistlib
    try:
        arguments = plistlib.loads(_launch_agent().read_bytes()).get('ProgramArguments', [])
        return arguments[arguments.index('--vault') + 1]
    except (OSError, ValueError, IndexError, plistlib.InvalidFileException):
        return None


def _windows_dirs():
    appdata = Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming')))
    programs = appdata / 'Microsoft/Windows/Start Menu/Programs'
    return programs, appdata / 'Microsoft/Windows/SendTo', programs / 'Startup'


def _windows_listener_link(vault):
    """This vault's entry in shell:startup. The hash in the name is the owner mark the macOS plist
    carries in its arguments: `kaldir` and `durum` only ever look at their own vault's file."""
    return _windows_dirs()[2] / (WIN_LISTENER + ' ' + _windows_digest(vault)[:8] + '.lnk')


def _windows_listener_running(vault):
    _user32, kernel32 = _win32()
    handle = kernel32.OpenMutexW(0x00100000, False, _windows_mutex(vault))  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def _windows_listener_ok(vault, process, timeout=8.0):
    """True once the listener holds this vault's mutex; False as soon as it has exited
    (the key is taken) or when the wait runs out."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _windows_listener_running(vault):
            return True
        if process.poll() is not None:
            break
        time.sleep(0.05)
    return _windows_listener_running(vault)


def _windows_stop_listener(vault=None, timeout=5.0):
    """Ask the session's listener to leave through its named event.

    No process id is involved, so nothing but a yakala listener can ever be stopped. With a
    vault only that vault's listener is asked (uninstall leaves another vault's alone); without
    one, whichever runs (`kur` moves the key to its vault, as the single LaunchAgent does).
    """
    import time
    if vault is not None and not _windows_listener_running(vault):
        return
    _user32, kernel32 = _win32()
    name = _windows_stop_event()
    event = kernel32.OpenEventW(0x0002, False, name)  # EVENT_MODIFY_STATE
    if not event:
        return
    kernel32.SetEvent(event)
    kernel32.CloseHandle(event)
    # The event lives while the listener holds it; once it is gone the next listener may start.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = kernel32.OpenEventW(0x00100000, False, name)  # SYNCHRONIZE
        if not event:
            return
        kernel32.CloseHandle(event)
        time.sleep(0.05)


def _ps_quote(value):
    # PowerShell also ends a single-quoted string at the typographic quotes (U+2018..U+201B).
    return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r'\1\1', str(value)) + "'"


def _windows_link_hotkey(spec):
    """`CTRL+ALT+B` as the number a shell link stores: virtual key low, HOTKEYF_* flags high."""
    flags, key = 0, 0
    for part in str(spec).upper().split('+'):
        if part in ('SHIFT', 'CTRL', 'ALT', 'EXT'):
            flags |= {'SHIFT': 1, 'CTRL': 2, 'ALT': 4, 'EXT': 8}[part]
        elif len(part) == 1 and part.isascii() and part.isalnum():
            key = ord(part)
        elif re.fullmatch(r'F([1-9]|1[0-9]|2[0-4])', part):
            key = 0x6F + int(part[1:])
        else:
            raise ValueError('Kisayol tusu taninmadi: ' + str(spec))
    return flags << 8 | key


def _windows_shortcut(path, target, arguments, hotkey=None):
    """Write a .lnk through the shell's own link object, which keeps every character.

    WScript.Shell turns each string into the ANSI code page first: on a cp1252 system a vault under
    `Şifre 📥` was stored as `Sifre ??`, so the entry started a path that does not exist, and a link
    whose own path had such a letter could not be saved at all (measured on the Windows runners).
    """
    import base64
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The link object loads what the file holds: start from an empty file, so a property this call
    # no longer sets (3.9.0 stored the key in the Start menu entry) cannot survive the rewrite.
    path.write_bytes(b'')
    lines = ["$ErrorActionPreference = 'Stop'",
             '$l = (New-Object -ComObject Shell.Application).NameSpace(' + _ps_quote(path.parent) + ').ParseName(' +
             _ps_quote(path.name) + ').GetLink',
             '$l.Path = ' + _ps_quote(target), '$l.Arguments = ' + _ps_quote(arguments),
             '$l.Description = ' + _ps_quote('Beyne at: ikinci beyne kaynak yakala'), '$l.ShowCommand = 7']
    if hotkey:  # only to rebuild what 3.9.0 left; `kur` gives the key to the listener instead
        lines.append('$l.Hotkey = ' + str(_windows_link_hotkey(hotkey)))
    lines.append('$l.Save()')
    encoded = base64.b64encode('\n'.join(lines).encode('utf-16le')).decode('ascii')
    try:
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                       check=True, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        if path.is_file() and not path.stat().st_size:
            path.unlink()  # an empty .lnk in Startup or Send To is an error dialog waiting to happen
        raise


def _argline(values):
    return subprocess.list2cmdline([str(v) for v in values])


DESKTOP_NAME = 'beyne-at.desktop'
GNOME_KEYS = 'org.gnome.settings-daemon.plugins.media-keys'
GNOME_PATH = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/beyne-at/'
KGLOBAL = ['gdbus', 'call', '--session', '-d', 'org.kde.kglobalaccel']
KDE_ACTION = "['beyne-at.desktop','_launch','Beyne At','Beyne At']"


def _desktop_file():
    data = os.environ.get('XDG_DATA_HOME') or ''
    # The basedir spec: a relative XDG_DATA_HOME is invalid and ignored (it would land under the working directory).
    return (Path(data) if os.path.isabs(data) else Path.home() / '.local/share') / 'applications' / DESKTOP_NAME


def _linux_desktop():
    current = os.environ.get('XDG_CURRENT_DESKTOP', '').upper()
    return 'kde' if 'KDE' in current else 'gnome' if 'GNOME' in current else None


def _desktop_string(value):
    """String value of a desktop entry: backslash, newline, tab and carriage return are escaped."""
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r')


def _desktop_quote(arg):
    """Exec argument per the Desktop Entry spec, then the string-value escaping of the file itself."""
    arg = str(arg)
    if not arg or re.search(r'[\s"\'\\><~|&;$*?#()`%]', arg):
        arg = '"' + re.sub(r'(["`$\\])', r'\\\1', arg).replace('%', '%%') + '"'
    return _desktop_string(arg)


def _desktop_entry(argv, vault):
    """The launcher file. X-Beyin-Vault names the vault it serves; `kaldir` and `durum` touch only their own."""
    return ('[Desktop Entry]\nType=Application\nName=Beyne at\nComment=İkinci beyne kaynak yakala\n'
            'Exec=' + ' '.join(_desktop_quote(a) for a in argv) + '\nTerminal=false\nCategories=Utility;\n'
            'X-Beyin-Vault=' + _desktop_string(vault) + '\n')


def _desktop_owner(path):
    """Vault the entry at `path` serves; None when it is missing, unreadable or not written by `kur`."""
    try:
        lines = Path(path).read_text(encoding='utf-8').split('\n')
    except (OSError, UnicodeDecodeError):
        return None
    unescape = {'n': '\n', 't': '\t', 'r': '\r', 's': ' '}
    for line in lines:
        if line.startswith('X-Beyin-Vault='):
            return re.sub(r'\\(.)', lambda match: unescape.get(match.group(1), match.group(1)), line.split('=', 1)[1])
    return None


def _gnome_list():
    code, out = _try(['gsettings', 'get', GNOME_KEYS, 'custom-keybindings'])
    if code != 0:
        return None
    try:
        import ast
        value = ast.literal_eval(out.strip().replace('@as ', '', 1))
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    # Anything but a list of strings is not rewritten: the user's own bindings live in it.
    return list(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else None


def _gvariant(text):
    """GVariant text of a string. A bare value that starts with a quote makes `gsettings set` fail."""
    return "'" + str(text).replace('\\', '\\\\').replace("'", "\\'") + "'"


def _gnome_set_list(paths):
    value = '[' + ', '.join(_gvariant(path) for path in paths) + ']'
    return _try(['gsettings', 'set', GNOME_KEYS, 'custom-keybindings', value])[0] == 0


def _gnome_schema():
    return GNOME_KEYS + '.custom-keybinding:' + GNOME_PATH


def _kde_active():
    code, out = _try(KGLOBAL + ['-o', '/component/beyne_at_desktop', '-m', 'org.kde.kglobalaccel.Component.isActive'])
    return code == 0 and 'true' in out


def _hand_over(vault, state):
    """The desktop entry starts the copy in the state folder. When the vault has its own module, run that
    one, so an update reaches the window without a second `kur` (the macOS listener does the same)."""
    here = Path(__file__).resolve()
    current = Path(vault) / '.claude/scripts' / here.name
    if here == (Path(state) / 'yakala' / here.name).resolve() and current.is_file() and current.resolve() != here:
        try:
            os.execv(sys.executable, [sys.executable, str(current)] + sys.argv[1:])
        except OSError:
            pass  # the copy still opens the window


def _linux_hotkey_install(kde, qt, gnome, command):
    """Registers the key on KDE or GNOME; True/False = key verified, None = desktop not supported."""
    desktop = _linux_desktop()
    if desktop == 'kde':
        writer = _which('kwriteconfig6', 'kwriteconfig5')
        if not writer or _try([writer, '--file', 'kglobalshortcutsrc', '--group', 'services', '--group', DESKTOP_NAME,
                               '--key', '_launch', kde])[0] != 0:
            return False
        _try([_which('kbuildsycoca6', 'kbuildsycoca5') or 'kbuildsycoca6'], timeout=30)
        _try(KGLOBAL + ['-o', '/kglobalaccel', '-m', 'org.kde.KGlobalAccel.doRegister', KDE_ACTION])
        # Flags 6 = SetPresent|NoAutoloading; with 4 alone the key is stored but never grabbed.
        _try(KGLOBAL + ['-o', '/kglobalaccel', '-m', 'org.kde.KGlobalAccel.setShortcutKeys', KDE_ACTION, '[([%d],)]' % qt, '6'])
        code, out = _try(KGLOBAL + ['-o', '/kglobalaccel', '-m', 'org.kde.KGlobalAccel.getGlobalShortcutsByKey', str(qt)])
        return code == 0 and DESKTOP_NAME in out
    if desktop == 'gnome':
        paths = _gnome_list()
        if paths is None:
            return False
        if GNOME_PATH not in paths and not _gnome_set_list(paths + [GNOME_PATH]):
            return False
        schema = _gnome_schema()
        for key, value in (('name', 'Beyne at'), ('command', command), ('binding', gnome)):
            if _try(['gsettings', 'set', schema, key, _gvariant(value)])[0] != 0:
                return False
        return GNOME_PATH in (_gnome_list() or [])
    return None


def install(vault, state, hotkey=True, spec=None, folder_spec=None):
    vault, state = Path(vault).resolve(), Path(state).resolve()
    spec = spec or saved_hotkey(state)
    # Validate before anything is written, so a bad key or folder changes nothing.
    if hotkey and sys.platform == 'darwin':
        keycode, modifiers, label = mac_hotkey(spec)
    elif hotkey and sys.platform.startswith('linux'):
        kde_key, qt_key, gnome_key, label = linux_hotkey(spec)
    elif hotkey and os.name == 'nt':
        win_value, label = windows_hotkey(spec)
    previous = find_inbox(vault, state)
    inbox_rel = previous if folder_spec is None else chosen_inbox(vault, folder_spec)
    if _same_folder(vault / inbox_rel, vault / previous):
        inbox_rel, left = previous, []  # one spelling for one folder (NFC typed for an NFD name, letter case)
    else:  # cards are never moved for the user; the ones still queued in the old folder are reported
        left = [card for card in cards(vault, folder=vault / previous) if card['meta'].get('durum') != 'islendi']
    folder = vault / inbox_rel
    folder.mkdir(parents=True, exist_ok=True)
    template = folder / (TEMPLATE_NAME.replace(' ', '-').lower() + '-web-clipper.json')
    template.write_text(json.dumps(clipper_template(inbox_rel), ensure_ascii=False, indent='\t') + '\n', encoding='utf-8')
    script = Path(__file__).resolve()
    done = {'status': 'kuruldu', 'klasor': inbox_rel, 'web_clipper_sablonu': template.relative_to(vault).as_posix(),
            'kisayol': None, 'gonder_menusu': False, 'skill': []}
    if left:
        done.update(onceki_klasor=previous, onceki_klasorde_kalan=len(left))
    # A vault without the beyin.py entry (standalone use) gets the direct module command.
    if (vault / 'beyin.py').is_file():
        prefix = 'python3 beyin.py yakala'
    elif vault in script.parents:
        prefix = 'python3 ' + script.relative_to(vault).as_posix() + ' --vault .'
    else:
        prefix = 'python3 "' + str(script) + '" --vault .'
    for root in ('.agents', '.claude'):
        skill = vault / root / 'skills/beyin-yakala/SKILL.md'
        if skill.exists() and SKILL_MARK not in skill.read_text(encoding='utf-8', errors='ignore'):
            continue  # the user's own skill with this name stays untouched
        folder_skill = skill.parent
        if root == '.claude' and not os.path.lexists(folder_skill) and _links_skills(folder_skill.parent):
            try:  # vaults that keep one source under .agents and link it from .claude
                folder_skill.symlink_to(Path('../../.agents/skills/beyin-yakala'), target_is_directory=True)
                done['skill'].append(folder_skill.relative_to(vault).as_posix() + ' -> .agents')
                continue
            except OSError:
                pass
        folder_skill.mkdir(parents=True, exist_ok=True)
        skill.write_text(SKILL_MD.replace('python3 beyin.py yakala', prefix).replace('📥 000-Inbox/Yakala', inbox_rel), encoding='utf-8', newline='\n')
        done['skill'].append(skill.relative_to(vault).as_posix())
    if hotkey and sys.platform == 'darwin':
        # The listener runs from a copy outside the vault: a synced folder may evict the original.
        runner = state / 'yakala' / script.name
        runner.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, runner)
        import plistlib
        plist = {'Label': LAUNCH_LABEL, 'RunAtLoad': True, 'KeepAlive': True,
                 'ProgramArguments': [sys.executable, str(runner), 'dinle', '--vault', str(vault),
                                      '--keycode', str(keycode), '--mods', str(modifiers)],
                 'ProcessType': 'Interactive', 'StandardErrorPath': str(state / 'yakala' / 'dinleyici.log'),
                 'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'),
                                          'LANG': 'en_US.UTF-8'}}
        agent = _launch_agent()
        previous = _agent_vault()
        if previous and previous != str(vault):
            done['onceki_vault'] = previous  # the hotkey moves to this vault
        agent.parent.mkdir(parents=True, exist_ok=True)
        domain = 'gui/' + str(os.getuid())
        subprocess.run(['launchctl', 'bootout', domain + '/' + LAUNCH_LABEL], capture_output=True)
        agent.write_bytes(plistlib.dumps(plist))
        subprocess.run(['launchctl', 'bootstrap', domain, str(agent)], check=True, capture_output=True)
        done['kisayol'] = label
        done['kisayol_calisiyor'] = _listener_ok()
    elif hotkey and sys.platform.startswith('linux'):
        runner = state / 'yakala' / script.name  # copy outside the vault, like the macOS listener
        runner.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, runner)
        argv = [sys.executable, str(runner), 'pencere', '--vault', str(vault)]
        entry = _desktop_file()
        previous = _desktop_owner(entry)
        if previous and previous != str(vault):
            done['onceki_vault'] = previous  # the hotkey moves to this vault
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(_desktop_entry(argv, vault), encoding='utf-8', newline='\n')
        import shlex
        command = shlex.join(argv)
        running = _linux_hotkey_install(kde_key, qt_key, gnome_key, command)
        if running is None:
            done['ipucu'] = 'Masaustu ayarlarindan su komuta bir kisayol bagla: ' + command
        else:
            done.update(kisayol=label, kisayol_calisiyor=running)
            if not running:  # a missing tool or a refused call is not an error: the command still works by hand
                done['ipucu'] = 'Kisayol dogrulanamadi; calismiyorsa masaustu ayarlarindan su komuta elle bagla: ' + command
        if not _linux_window():
            done['uyari'] = LINUX_NO_WINDOW
    elif hotkey and os.name == 'nt':
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        target = pythonw if pythonw.is_file() else Path(sys.executable)
        programs, sendto, startup = _windows_dirs()
        # No .Hotkey on the Start menu entry: the listener owns the combination. One combination has
        # one owner, so with both either Explorer or the listener would fail after the next logon.
        _windows_shortcut(programs / 'Beyne At.lnk', target, _argline([script, 'pencere', '--vault', vault]))
        _windows_shortcut(sendto / 'Beyne At.lnk', target, _argline([script, 'ekle', '--vault', vault, '--arac', 'gonder-menusu']))
        # One listener per logon session, like the single LaunchAgent: the key moves to this vault.
        _windows_stop_listener()
        for other in startup.glob(WIN_LISTENER + '*.lnk'):
            other.unlink()
        runner = state / 'yakala' / script.name
        runner.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, runner)
        command = [str(part) for part in (runner, 'dinle', '--vault', vault, '--tus', spec)]
        _windows_shortcut(_windows_listener_link(vault), target, _argline(command))
        # Null handles and a folder of its own: the listener outlives this command, and a process
        # that kept the caller's pipes or stood inside the vault would block both (no rename, no move).
        listener = subprocess.Popen([str(target)] + command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, cwd=str(runner.parent), creationflags=NO_WINDOW)
        done.update(kisayol=label, gonder_menusu=True)
        done['kisayol_calisiyor'] = _windows_listener_ok(vault, listener)
    state.mkdir(parents=True, exist_ok=True)
    _state_path(state).write_text(json.dumps({'schema': 1, 'session_notice': True, 'kisayol': done['kisayol'],
                                              'tus': spec if done['kisayol'] else saved_hotkey(state),
                                              'klasor': inbox_rel}, ensure_ascii=False) + '\n',
                                  encoding='utf-8')
    done['araclar'] = {name: bool(path) for name, path in tools().items()}
    return done


def uninstall(vault, state):
    state = Path(state).resolve()
    removed = []
    if sys.platform == 'darwin' and _agent_vault() == str(Path(vault).resolve()):
        # Only the listener this vault installed: one hotkey serves one vault at a time.
        subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + LAUNCH_LABEL], capture_output=True)
        _launch_agent().unlink()
        removed.append('LaunchAgent')
    elif sys.platform.startswith('linux') and _desktop_owner(_desktop_file()) == str(Path(vault).resolve()):
        _desktop_file().unlink()
        removed.append(DESKTOP_NAME)
        desktop = _linux_desktop()
        if desktop == 'kde':
            writer = _which('kwriteconfig6', 'kwriteconfig5')
            if writer:
                _try([writer, '--file', 'kglobalshortcutsrc', '--group', 'services', '--group', DESKTOP_NAME,
                      '--key', '_launch', '--delete'])
            _try(KGLOBAL + ['-o', '/kglobalaccel', '-m', 'org.kde.KGlobalAccel.unregister', DESKTOP_NAME, '_launch'])
        elif desktop == 'gnome':
            paths = _gnome_list() or []
            if GNOME_PATH in paths:
                _gnome_set_list([p for p in paths if p != GNOME_PATH])
            _try(['gsettings', 'reset-recursively', _gnome_schema()])
    elif os.name == 'nt':
        # Only this vault's listener and startup entry. When another vault's entry is there instead,
        # its `kur` rewrote the Start menu and Send To shortcuts too: they are no longer this vault's.
        _windows_stop_listener(vault)
        programs, sendto, startup = _windows_dirs()
        own = _windows_listener_link(vault)
        moved = not own.exists() and any(startup.glob(WIN_LISTENER + '*.lnk'))
        for link in ([] if moved else [programs / 'Beyne At.lnk', sendto / 'Beyne At.lnk']) + [own]:
            if link.exists():
                link.unlink()
                removed.append(str(link.name))
    link = Path(vault) / '.claude/skills/beyin-yakala'
    if link.is_symlink() and os.readlink(link).replace('\\', '/').endswith('.agents/skills/beyin-yakala'):
        link.unlink()
        removed.append('.claude/skills/beyin-yakala')
    for root in ('.agents', '.claude'):
        skill = Path(vault) / root / 'skills/beyin-yakala/SKILL.md'
        if skill.is_file() and SKILL_MARK in skill.read_text(encoding='utf-8', errors='ignore'):
            skill.unlink()
            try:
                skill.parent.rmdir()
            except OSError:
                pass
            removed.append(root + '/skills/beyin-yakala')
    runner = state / 'yakala' / Path(__file__).name
    if runner.exists():
        runner.unlink()
    if _state_path(state).exists():
        _state_path(state).unlink()
        removed.append(STATE_FILE)
    return {'status': 'kaldirildi', 'kaldirilan': removed, 'notlar_korundu': True}


def status(vault, state):
    installed = _state_path(state).is_file()
    running = None
    if sys.platform == 'darwin' and _agent_vault() == str(Path(vault).resolve()):
        running = _listener_ok(wait=0)
    elif os.name == 'nt' and _windows_listener_link(vault).exists():
        # None without this vault's startup entry: a 3.9.0 install or --kisayol-yok has no listener.
        running = _windows_listener_running(vault)
    elif sys.platform.startswith('linux') and _desktop_owner(_desktop_file()) == str(Path(vault).resolve()):
        desktop = _linux_desktop()
        if desktop == 'kde':
            running = _kde_active()
        elif desktop == 'gnome':
            running = GNOME_PATH in (_gnome_list() or [])
    return {'status': 'tamam', 'kurulu': installed, 'dinleyici_calisiyor': running,
            'bekleyen': pending(vault, state), 'klasor': find_inbox(vault, state),
            'araclar': {name: bool(path) for name, path in tools().items()}}


# ---------------------------------------------------------------- CLI

def default_state(vault):
    import hashlib
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support'
    else:
        base = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    return base / 'beyin-v3' / hashlib.sha256(str(vault).encode()).hexdigest()[:16]


def resolve_state(vault):
    config = Path(vault) / '.beyin-runtime.json'
    try:
        pinned = Path(json.loads(config.read_text(encoding='utf-8'))['state']).expanduser()
    except (OSError, ValueError, KeyError, TypeError):
        pinned = None
    # A pin from another OS through a synced vault is not absolute here (#249).
    return pinned if pinned and pinned.is_absolute() else default_state(Path(vault).resolve())


def human(result, command):
    if result.get('status') == 'yakalandi':
        return 'Beyne atildi: ' + result['path']
    if result.get('status') == 'mevcut_karta_eklendi':
        return 'Bu kaynak zaten vardi; yeni notun karta eklendi: ' + result['path']
    if command == 'isle':
        lines = []
        for entry in result['kartlar']:
            mark = {'cikarildi': 'tamam', 'hata': 'HATA'}.get(entry['durum'], entry['durum'])
            lines.append('[' + mark + '] ' + str(entry.get('baslik') or entry['id'])[:70] +
                         ((' (' + entry['yontem'] + ')') if entry.get('yontem') else '') +
                         (('\n    ' + entry['not']) if entry.get('not') else ''))
        return '\n'.join(lines) or 'Bekleyen kaynak yok.'
    if command == 'liste':
        rows = result['kartlar']
        return '\n'.join('%-10s %-8s %s' % (r['durum'], r['kaynak_turu'], str(r['baslik'])[:70]) for r in rows) or 'Yakalanan kaynak yok.'
    if command == 'kisayol' and result.get('status') == 'kisayol':
        if not result.get('kurulu'):
            return 'Yakala kurulu degil. Kurmak icin: beyin.py yakala kur'
        return 'Kisayol: ' + str(result.get('kisayol') or 'yok') + '\nDegistirmek icin: beyin.py yakala kisayol \'cmd+"\''
    if command in ('kur', 'kisayol'):
        lines = ['Kisayol degisti.' if command == 'kisayol' else 'Yakala kuruldu.']
        if result.get('kisayol'):
            lines.append('Kisayol: ' + result['kisayol'] + ' (her uygulamada calisir).')
        if result.get('kisayol_calisiyor') is False:
            lines.append('UYARI: kisayol dinleyicisi baslamadi; bu tus baska bir uygulamada kayitli olabilir. Baska bir tus dene.')
        if result.get('ipucu'):
            lines.append(result['ipucu'])
        if result.get('uyari'):
            lines.append('UYARI: ' + result['uyari'])
        if command == 'kisayol':
            return '\n'.join(lines)
        if result.get('onceki_klasorde_kalan'):
            lines.append('UYARI: ' + str(result['onceki_klasorde_kalan']) + ' kart eski klasorde kaldi (' + result['onceki_klasor'] +
                         '); tasinmadi. Islenmeleri icin yeni klasore tasi: ' + result['klasor'])
        if result.get('gonder_menusu'):
            lines.append('Dosyalar icin: sag tik > Gonder > Beyne At.')
        lines.append('Tarayici icin Obsidian Web Clipper eklentisini kur, Ayarlar > Sablonlar > Ice aktar ile su dosyayi sec:')
        lines.append('  ' + result['web_clipper_sablonu'])
        missing = [name for name, ok in result.get('araclar', {}).items() if not ok]
        if missing:
            lines.append('Istege bagli eksik araclar: ' + ', '.join(missing) + ' (video altyazisi ve ses icin; yoksa elde olanla devam edilir).')
        return '\n'.join(lines)
    if command == 'kaldir':
        return 'Yakala kaldirildi. Yakalanan notlar yerinde duruyor.'
    if command == 'durum':
        return ('Kurulu: ' + ('evet' if result['kurulu'] else 'hayir') +
                ('' if result['dinleyici_calisiyor'] is None else '\nKisayol dinleyicisi: ' +
                 ('calisiyor' if result['dinleyici_calisiyor'] else 'durmus (baslatmak icin: beyin.py yakala kur)')) +
                '\nBekleyen kaynak: ' + str(result['bekleyen']) +
                '\nAraclar: ' + ', '.join(n + ('=var' if ok else '=yok') for n, ok in result['araclar'].items()))
    if result.get('status') == 'islendi':
        return 'Kart islendi olarak isaretlendi: ' + result['id']
    return json.dumps(result, ensure_ascii=False, indent=2)


def _json_text(result, stream):
    """JSON for `stream`: readable where it already writes UTF-8, ASCII escapes everywhere else.

    macOS and Linux keep the literal characters 3.9.0 printed (a Turkish title, the inbox emoji).
    A piped Windows console is cp1254 or cp1252, where the emoji cannot be encoded at all, and
    ASCII means the same under whatever decoder the reader uses (#268). The stream decides, not
    the platform: its encoding name is normalized (utf-8, utf8, UTF8, cp65001), and a detached
    or closed stream, or one without a usable name, gets ASCII.
    """
    import codecs
    try:
        # .closed raises on a detached stream, which still reports the encoding it once had.
        if not stream.closed and codecs.lookup(stream.encoding).name == 'utf-8':
            text = json.dumps(result, ensure_ascii=False, indent=2)
            text.encode('utf-8')  # a lone surrogate (an undecodable file name) still needs the escapes
            return text
    except (AttributeError, LookupError, TypeError, ValueError):
        pass
    return json.dumps(result, ensure_ascii=True, indent=2)


def main(argv=None, vault=None, state=None):
    def shared(default):
        # Subcommands must not reset a --vault/--json given before them, hence SUPPRESS there.
        common = argparse.ArgumentParser(add_help=False)
        common.add_argument('--vault', type=Path, dest='vault_sub', default=default, help=argparse.SUPPRESS)
        common.add_argument('--json', action='store_true', dest='force_json',
                            default=False if default is None else default, help=argparse.SUPPRESS)
        return common
    parser = argparse.ArgumentParser(prog='beyin.py yakala', description='Kaynak yakala ve ikinci beyne isle', parents=[shared(None)])
    sub = parser.add_subparsers(dest='command')
    common = shared(argparse.SUPPRESS)

    def command_parser(name, **options):
        return sub.add_parser(name, parents=[common], **options)
    add = command_parser('ekle', help='URL, metin ya da dosya yakala (ag yok)')
    add.add_argument('items', nargs='*', help='URL, dosya yolu ya da metin')
    add.add_argument('--neden', default='')
    add.add_argument('--metin')
    add.add_argument('--baslik')
    add.add_argument('--arac', default='komut')
    window = command_parser('pencere', help='Yakalama penceresini ac')
    window.add_argument('--baglam', help=argparse.SUPPRESS)
    listen = command_parser('dinle', help=argparse.SUPPRESS)
    listen.add_argument('--keycode', type=int, default=11)
    listen.add_argument('--mods', type=int, default=0x1000 | 0x800)
    listen.add_argument('--tus')
    run = command_parser('isle', help='Bekleyen kaynaklarin metnini cikar (ag kullanir)')
    run.add_argument('ids', nargs='*')
    run.add_argument('--ses-yok', action='store_true', help='Altyazi yoksa sesi indirme')
    run.add_argument('--tekrar', action='store_true', help='Hata alan kartlari da yeniden dene')
    done = command_parser('bitti', help='Karti islendi olarak isaretle')
    done.add_argument('id')
    done.add_argument('--bilgi', action='append', default=[], help='Olusan ya da guncellenen bilgi notu (vault-goreli)')
    done.add_argument('--ozet')
    show = command_parser('liste', help='Yakalanan kaynaklar')
    show.add_argument('--durum', choices=STATUSES)
    setup = command_parser('kur', help='Kisayolu, Web Clipper sablonunu, skill\'i ve oturum bildirimini kur')
    setup.add_argument('--kisayol-yok', action='store_true', help='Yalniz sablon, skill ve bildirim; kisayol kurulmaz')
    setup.add_argument('--tus', help='Kisayol, ornek: ctrl+alt+b (varsayilan) ya da cmd+"')
    setup.add_argument('--klasor', help='Kartlarin yazilacagi klasor, vault icinde (ornek: 000-Inbox/Yakala)')
    keys = command_parser('kisayol', help='Kisayolu goster ya da degistir, ornek: kisayol \'cmd+"\'')
    keys.add_argument('tus', nargs='?')
    command_parser('kaldir', help='Kisayolu ve bildirimi kaldir; notlara dokunmaz')
    command_parser('durum', help='Kurulum ve arac durumu')
    command_parser('sablon', help='Web Clipper sablonunu yazdir')
    args = parser.parse_args(argv)
    vault = vault or args.vault_sub
    if vault is None:
        parser.error('--vault gerekli')
    vault = Path(vault).expanduser().resolve()
    state = Path(state) if state else resolve_state(vault)
    command = args.command or 'pencere'
    as_json = args.force_json or not sys.stdout or not sys.stdout.isatty()
    if command == 'pencere':
        if argv is None and sys.platform.startswith('linux'):
            _hand_over(vault, state)  # started by the hotkey from the state copy
        context = json.loads(args.baglam) if getattr(args, 'baglam', None) else gather_context()
        result = popup(vault, context, state)
    elif command == 'dinle':
        if sys.platform == 'darwin':
            listen_mac(vault, Path(__file__).resolve(), args.keycode, args.mods)
            return 0
        elif os.name == 'nt':
            return listen_windows(vault, Path(__file__).resolve(), args.tus, state)
        raise ValueError('Bu isletim sisteminde kisayol dinleyicisi desteklenmiyor')
    elif command == 'ekle':
        url, files, texts = None, [], []
        for item in args.items:
            if re.match(r'^https?://', item) and not url:
                url = item
            elif Path(item).expanduser().is_file():
                files.append(item)
            else:
                texts.append(item)
        text = args.metin or ('\n'.join(texts) if texts else None)
        result = capture(vault, url=url, text=text, files=files, why=args.neden, title=args.baslik, tool=args.arac, state=state)
    elif command == 'isle':
        result = process(vault, args.ids or None, allow_audio=not args.ses_yok, retry=args.tekrar, state=state)
    elif command == 'bitti':
        result = finish(vault, args.id, args.bilgi, args.ozet, state=state)
    elif command == 'liste':
        result = listing(vault, args.durum, state)
    elif command == 'kur':
        result = install(vault, state, hotkey=not args.kisayol_yok, spec=args.tus, folder_spec=args.klasor)
    elif command == 'kisayol':
        if args.tus:
            result = install(vault, state, spec=args.tus)
            result['status'] = 'kisayol_degisti'
        else:
            installed = json.loads(_state_path(state).read_text(encoding='utf-8')) if _state_path(state).is_file() else {}
            result = {'status': 'kisayol', 'kisayol': installed.get('kisayol'), 'tus': installed.get('tus') or DEFAULT_HOTKEY,
                      'kurulu': bool(installed)}
    elif command == 'kaldir':
        result = uninstall(vault, state)
    elif command == 'durum':
        result = status(vault, state)
    else:
        result = clipper_template(find_inbox(vault, state))
    if sys.stdout is None:
        return 0
    if as_json or command == 'sablon':
        # Keep the stream check: literal characters only for a UTF-8 stream, ASCII escapes for a
        # legacy code page, where printing them raw fails the command after it did its work (#268).
        print(_json_text(result, sys.stdout))
        return 0
    if hasattr(sys.stdout, 'reconfigure'):
        # Text for a person: a character the terminal cannot show prints as '?', as in beyin.py.
        sys.stdout.reconfigure(errors='replace')
    print(human(result, command))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValueError as exc:
        if sys.stderr:
            print(str(exc), file=sys.stderr)
        raise SystemExit(1)
