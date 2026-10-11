"""Read-preserving V2 cutover guards and receipts; no legacy replay/model calls."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

LEGACY_RUNNERS = ['.claude/scripts/flush.py', '.claude/scripts/compile.py'] + ['.claude/hooks/'+name+suffix for name in ('session-start', 'session-end', 'pre-compact', 'prompt-counter') for suffix in ('.sh', '.ps1')]

# How old a flush sentinel has to be, by the start time it recorded, before a free lock is taken
# as proof that its writer died. A stock V2 flush is done within about ten minutes of taking its
# lock (two model attempts of at most 270 seconds each), a little later when it first waited for
# another flush of the same session, and V2 itself sweeps a flush's hook input after this same
# hour (STALE_HOOK_INPUT_SECONDS in flush.py).
ORPHANED_FLUSH_SECONDS = 3600
RECONCILE_HINT = ' with no V2 writer running, set its status to a terminal value such as "fail" and rerun'


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_root(vault):
    path = vault/'.claude/scripts/.state'
    cursor = path
    while cursor != vault:
        if cursor.is_symlink():
            raise RuntimeError('legacy state symlink requires explicit reconciliation')
        cursor = cursor.parent
    return path


def _inventory(vault):
    from beyin_v3_sync import EXCLUDED_FILES
    sources = {}
    for directory, dirs, files in os.walk(vault, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith('.') and name != 'node_modules' and not (Path(directory)/name).is_symlink()]
        for name in files:
            p = Path(directory)/name
            if name.lower().endswith('.md') and name.casefold() not in EXCLUDED_FILES and not p.is_symlink():
                sources[p.relative_to(vault).as_posix()] = _hash(p)
    legacy = _legacy_root(vault)
    # Lock files are coordination artifacts, not user state. On Windows their
    # mandatory locks also make reopening them for hashing fail against the
    # installer process's own migration guard.
    states = {p.relative_to(vault).as_posix(): _hash(p) for p in legacy.rglob('*')
              if p.is_file() and not p.is_symlink() and p.suffix.casefold() != '.lock'} if legacy.exists() else {}
    return sources, states


@contextmanager
def _legacy_lock(path):
    with path.open('r+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise RuntimeError('legacy writer lock is active; wait for completion') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def migration_guard(vault_root, state_dir):
    vault, state = Path(vault_root).resolve(), Path(state_dir).resolve()
    if not vault.is_dir():
        raise ValueError('existing vault required')
    if state.is_relative_to(vault):
        raise ValueError('migration state must be outside vault')
    legacy = _legacy_root(vault)
    orphaned = []
    with ExitStack() as stack:
        if legacy.exists():
            held = set()
            for path in sorted(legacy.glob('*.lock')):
                if path.is_symlink():
                    raise RuntimeError('legacy writer lock symlink requires review')
                stack.enter_context(_legacy_lock(path))
                held.add(path.name)
            now = time.time()
            for path in legacy.glob('*.json'):
                if not path.name.startswith(('flush-', 'compile', 'antigravity-')):
                    continue
                try:
                    item = json.loads(path.read_text(encoding='utf-8'))
                except (ValueError, UnicodeError):
                    raise RuntimeError('legacy writer state unreadable; review locally')
                if isinstance(item, dict) and any(item.get(key) in ('inflight', 'running', 'pending') for key in ('status', 'state')):
                    relative = path.relative_to(vault).as_posix()
                    # Every V2 release that writes status "inflight" to flush-<key>.json takes
                    # flush-<key>.lock first and keeps it until its terminal status, so on this
                    # machine holding that lock proves the writer is gone (a logout or shutdown
                    # mid-summary), which the sentinel's age alone never was. No other record or
                    # state has that guarantee: stock V2 writes none of them, so whatever left
                    # one follows rules this guard does not know.
                    if (path.name.startswith('flush-') and item.get('status') == 'inflight'
                            and item.get('state') not in ('inflight', 'running', 'pending')
                            and path.with_suffix('.lock').name in held):
                        # The lock is only as wide as this machine and this lock interface, and
                        # the state directory travels with a synced vault: a flush that is still
                        # running on another machine leaves the same sentinel beside a lock that
                        # is free here, and one under WSL next to a Windows Python may too. Such a
                        # writer is done within minutes of the start time it recorded, so the proof
                        # also needs that time to be an hour old. A record without it is not a
                        # stock sentinel.
                        started = item.get('ts')
                        if (isinstance(started, (int, float)) and not isinstance(started, bool)
                                and 0 <= started <= now - ORPHANED_FLUSH_SECONDS):
                            orphaned.append(relative)
                            continue
                        raise RuntimeError('legacy writer inflight sentinel in ' + relative +
                                           '; its lock is free here, but its start time (ts) is missing or less than '
                                           + str(ORPHANED_FLUSH_SECONDS // 60) + ' minutes old, so a flush on another'
                                           ' machine that syncs this vault is not ruled out. Rerun once it is older,'
                                           ' or reconcile that record before migration:' + RECONCILE_HINT)
                    raise RuntimeError('legacy writer inflight sentinel in ' + relative +
                                       '; verify the prior write completed, then reconcile that record before migration:'
                                       + RECONCILE_HINT)
        sources, states = _inventory(vault)
        prior = state/'v2-migration.json'
        plan = {'migration': 'v2-to-v3-source-cutover-1', 'cutover_at': datetime.now(timezone.utc).isoformat(),
                'sources': sources, 'legacy_state': states,
                'historical_receipts': sorted(p.relative_to(vault).as_posix() for p in (vault/'receipts').glob('*.md'))}
        if orphaned:
            plan['orphaned_legacy_state'] = sorted(orphaned)
        if prior.exists():
            plan['previous'] = json.loads(prior.read_text(encoding='utf-8'))
        # Runners the user kept at install time stay theirs on every later update.
        manifest = state/'v3-install.json'
        if manifest.exists():
            kept = json.loads(manifest.read_text(encoding='utf-8')).get('kept_legacy', [])
            if kept:
                plan['kept_legacy'] = sorted(kept)
        yield plan


def finalize_migration(vault_root, state_dir, plan):
    from beyin_v3_sync import atomic
    vault, state = Path(vault_root).resolve(), Path(state_dir).resolve()
    kept = set(plan.get('kept_legacy', []))
    for name in LEGACY_RUNNERS:
        path = vault/name
        if path.is_symlink():
            raise RuntimeError('legacy runner symlink requires explicit reconciliation')
        if name in kept:
            continue
        if path.exists() and ('BEYIN_V3_LEGACY_RETIRED' not in path.read_text(encoding='utf-8')):
            raise RuntimeError('legacy runner not retired; use supported installer cutover')
    if plan.get('previous'):
        # An earlier cutover already left its success receipt, so there is nothing to prove or
        # write here. The plan's source digests may be old (recovering an interrupted update),
        # and a note edited since must not make recovery impossible.
        return plan['previous']
    sources, states = _inventory(vault)
    if any(sources.get(name) != digest for name, digest in plan['sources'].items()) or states != plan['legacy_state']:
        raise RuntimeError('source or legacy state changed during migration; no success receipt')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    written = []
    try:
        for name in plan['legacy_state']:
            destination = state/'v2-preserved-state'/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Recorded before the copy: a copy that fails half way leaves a partial file too.
            written.append(destination)
            shutil.copyfile(vault/name, destination)
            destination.chmod(0o600)
        result = dict(plan, status='succeeded', historical_replay=False,
                      compiler_policy='New outcomes use explicit semantic receipts and knowledge notes; no automatic model compiler. External schedules require operator review.')
        atomic(state/'v2-migration.json', json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except Exception:
        # Rollback partial state copy
        for path in written:
            path.unlink(missing_ok=True)
        raise


def migrate_v2(vault_root, state_dir):
    with migration_guard(vault_root, state_dir) as plan:
        return finalize_migration(vault_root, state_dir, plan)
