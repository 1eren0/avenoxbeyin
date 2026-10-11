"""Directory names the user keeps out of the source index. Machine-local, no models.

`<state>/index.json` holds `{"excluded_dirs": ["3rdparty", "target"]}`: bare directory names
that the sync walk never descends into, at any depth. The engine prunes nothing by name on
its own beyond beyin_v3_sync.EXCLUDED_DIRS: "Vendor" or "External" is a vendored code tree
in one vault and a folder of supplier notes in another, so the user decides and every active
name is shown by `sync` and `doctor`. This is an index setting, not a privacy boundary: a
release before this file ignores it and indexes everything again; `visibility: private`
is what keeps a source out of context.
"""
import json
import os
from pathlib import Path
import stat
import tempfile
import unicodedata

from beyin_v3_companion import DEFAULT_DIRECTORY, directory as companion_directory

CONFIG_NAME = 'index.json'
MAX_CONFIG_BYTES = 65536
MAX_EXCLUDED_DIRS = 64
MAX_NAME_CHARS = 255
REPORT_LIMIT = 20
# Folders the engine reads back through the index: note-create and task-create verify the new
# source in it, and the session opening reads the companion files from it. A name-only list
# cannot tell the top-level folder from a nested one, so these names are refused outright.
PROTECTED_DIRS = ('knowledge', 'notes', 'tasks')
# Names that usually mean a copy of someone else's code. Never pruned on their own; doctor
# points at them only when the tree also carries upstream documents, which a notes folder
# that merely shares the name (supplier notes under "Vendor") does not.
VENDORED_HINT_DIRS = frozenset({'3rdparty', 'third_party', 'third-party', 'thirdparty', 'vendor', 'vendored', 'external'})
UPSTREAM_DOCS = frozenset({'readme.md', 'security.md', 'code_of_conduct.md', 'contributing.md', 'changelog.md', 'license.md'})


def dir_key(name):
    """One comparison key per directory name, whatever case or Unicode form it arrives in.

    macOS may hand out NFD names and the common volumes are case-insensitive. Turkish dotted
    and dotless i fold together (U+0130, I, U+0131 and i are one letter here), so a name typed
    in any case matches the folder; str.casefold() alone keeps U+0130 and U+0131 apart from i.
    """
    if name.isascii():
        return name.casefold()
    text = unicodedata.normalize('NFC', name).replace('\u0130', 'i').replace('\u0131', 'i')
    return unicodedata.normalize('NFC', text.casefold().replace('i\u0307', 'i'))


_PROTECTED_KEYS = frozenset(dir_key(name) for name in PROTECTED_DIRS + (DEFAULT_DIRECTORY,))


def companion_key(vault, keys):
    """The key of this vault's companion folder when the list names it, else None.

    The folder is resolved only when a top-level entry carries a listed name. A vault with
    thousands of files at its root then pays one directory listing per sync for this check,
    not a stat per entry.
    """
    if vault is None or not keys:
        return None
    try:
        with os.scandir(vault) as entries:
            if not any(dir_key(entry.name) in keys for entry in entries):
                return None
        target = companion_directory(vault)
    except OSError:
        return None  # an unreadable vault fails in the walk itself
    key = dir_key(target.name) if target is not None else None
    return key if key in keys else None


def check(value, vault=None):
    """The validated name list, first spelling of each name kept; ValueError says what is wrong.

    All or nothing: one bad entry refuses the file, so a typo is reported instead of half applied.
    """
    if not isinstance(value, dict) or set(value) - {'schema', 'excluded_dirs'} or value.get('schema', 1) != 1:
        raise ValueError('index settings accept only excluded_dirs')
    names = value.get('excluded_dirs', [])
    if not isinstance(names, list) or len(names) > MAX_EXCLUDED_DIRS:
        raise ValueError(f'excluded_dirs must be a list of at most {MAX_EXCLUDED_DIRS} directory names')
    result, seen = [], {}
    for name in names:
        if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > MAX_NAME_CHARS:
            raise ValueError('excluded_dirs entries must be directory names without surrounding spaces: ' + repr(name)[:80])
        if '/' in name or '\\' in name or name in ('.', '..') or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError('excluded_dirs takes bare directory names, matched at any depth, not paths: ' + repr(name)[:80])
        key = dir_key(name)
        if key not in seen:
            seen[key] = name
            result.append(name)
    refused = next((key for key in seen if key in _PROTECTED_KEYS), None) or companion_key(vault, seen)
    if refused is not None:
        raise ValueError('this folder is read back through the index and cannot be excluded: ' + repr(seen[refused])[:80])
    return result


def read(state, vault=None):
    """(names, error, present). No file: no names, no error. A file that cannot be applied:
    no names and the reason, so the caller indexes everything and can say why."""
    path = Path(state) / CONFIG_NAME
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return [], None, False
    except OSError as exc:
        return [], 'unreadable: ' + type(exc).__name__, True
    if not stat.S_ISREG(status.st_mode):
        return [], 'must be a regular file, not a link or a directory', True
    if status.st_size > MAX_CONFIG_BYTES:
        return [], f'larger than {MAX_CONFIG_BYTES} bytes', True
    try:
        # utf-8-sig: Windows PowerShell 5.1 and Notepad write a BOM.
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except OSError as exc:
        return [], 'unreadable: ' + type(exc).__name__, True
    except ValueError:  # json.JSONDecodeError and UnicodeDecodeError
        return [], 'not valid JSON', True
    try:
        return check(value, vault), None, True
    except ValueError as exc:
        return [], str(exc), True


def exclusions(state, vault=None):
    """(keys, report) for one sync. The report is None while no index.json exists, so a vault
    that never set this keeps its exact sync output."""
    names, error, present = read(state, vault)
    if not present:
        return frozenset(), None
    report = {'excluded_dirs': names, 'pruned': 0, 'pruned_dirs': []}
    if error is not None:
        report['error'] = error
    return frozenset(dir_key(name) for name in names), report


def prune(root, directory, dirs, keys, report):
    """Drop the excluded names from one os.walk level and note what was skipped."""
    kept = []
    for name in dirs:
        if dir_key(name) not in keys:
            kept.append(name)
            continue
        report['pruned'] += 1
        if len(report['pruned_dirs']) < REPORT_LIMIT:
            report['pruned_dirs'].append((Path(directory) / name).relative_to(root).as_posix())
    return kept


def plan(state, vault, add=(), remove=()):
    """The list after removing and adding names, validated as a whole; writes nothing."""
    names, error, _present = read(state, vault)
    if error is not None:
        raise ValueError(CONFIG_NAME + ' in the runtime state is invalid (' + error + '); fix or remove it first')
    dropped = {dir_key(name) for name in remove}
    names = [name for name in names if dir_key(name) not in dropped]
    names += [unicodedata.normalize('NFC', name) for name in add]
    return check({'excluded_dirs': names}, vault)


def save(state, vault, add=(), remove=()):
    result = plan(state, vault, add, remove)
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.index-', dir=state)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as out:
            json.dump({'schema': 1, 'excluded_dirs': result}, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, state / CONFIG_NAME)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result


def vendored_hint(sources, limit=5):
    """Indexed notes under a vendored-looking directory that also holds upstream documents
    (README, SECURITY, CONTRIBUTING ...). Information for doctor; None when there is none."""
    found = {}
    for source in sources:
        parts = source.split('/')
        for depth, part in enumerate(parts[:-1]):
            if dir_key(part) in VENDORED_HINT_DIRS:
                entry = found.setdefault('/'.join(parts[:depth + 1]), [0, False, part])
                entry[0] += 1
                entry[1] = entry[1] or parts[-1].casefold() in UPSTREAM_DOCS
                break
    rows = sorted(((path, count, name) for path, (count, upstream, name) in found.items() if upstream),
                  key=lambda row: (-row[1], row[0]))
    if not rows:
        return None
    names = {}
    for _path, _count, name in rows:
        names.setdefault(dir_key(name), name)
    return {'notes': sum(count for _path, count, _name in rows),
            'dirs': [{'dir': path, 'notes': count} for path, count, _name in rows[:limit]],
            'names': sorted(names.values(), key=dir_key), 'truncated': len(rows) > limit}


def doctor(state, vault, sources=()):
    """Read-only report: the active list, a file that is not applied, and the vendored hint.

    `sources` are the indexed source paths. Notes under an already listed name are left out of
    the hint: they leave the index with the next sync."""
    names, error, _present = read(state, vault)
    report = {'file': CONFIG_NAME, 'excluded_dirs': names,
              'status': 'invalid' if error is not None else 'active' if names else 'none'}
    if error is not None:
        report['error'] = error
    keys = {dir_key(name) for name in names}
    hint = vendored_hint(source for source in sources
                         if not any(dir_key(part) in keys for part in source.split('/')[:-1]))
    if hint:
        report['vendored_hint'] = hint
    return report
