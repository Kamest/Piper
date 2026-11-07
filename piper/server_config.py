"""Laptop snapshots for setup steps 7/9. Payloads live only for one request, never on disk."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile

import common
import sessioncfg
import codexcfg

MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 10000
# development/ holds notes next to screenshots, builds and recordings (often gigabytes): only text up to
# DEV_FILE is mirrored, and what does not fit is counted in one summary row instead of failing the step.
DEV_FILE = 1024 * 1024
DEV_TEXT = frozenset(('.md', '.markdown', '.txt', '.http', '.rest', '.json', '.yaml', '.yml', '.toml', '.csv', '.tsv',
                      '.xml', '.html', '.css', '.svg', '.sql', '.py', '.sh', '.js', '.mjs', '.ts', '.tsx', '.java', '.kt',
                      '.swift', '.properties', '.ini', '.cfg', '.conf', '.puml', '.mmd', '.diff', '.patch'))
DEV_SKIP_DIRS = frozenset(('build', 'dist', 'target', 'out', '.venv', 'venv', '.gradle', 'DerivedData', 'coverage'))
ENV_KEY = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')
PRIVATE_FIELD = re.compile(r'(?i)secret|token|credential|password|provider|pairing|(?:^|_)jobs?(?:_|$)|scheduled|authorization|api[_-]?key|private[_-]?key|^auth$')


def excluded(name):
    """Conservative filename boundary, shared with the bare peer's equivalent check."""
    lower = name.lower()
    if (lower.startswith(('.env', 'id_')) or lower in {'auth.json', '.claude.json', 'settings.local.json',
            'http-client.private.env.json', 'plugins', '.git', '__pycache__', 'node_modules'} or
            lower.endswith(('.pem', '.key', '.p12', '.sql.gz', '.dump')) or
            re.search(r'token|secret|credential|password', lower)):
        return 'secret or unsafe name'
    return None


def dev_text(path):
    """Whether a development/ file is the kind setup mirrors (by name; size is checked when reading)."""
    parts = Path(path).parts
    return Path(path).suffix.lower() in DEV_TEXT and not any(x in DEV_SKIP_DIRS for x in parts)


def filtered(value):
    """Export policy/project data without machine credentials, providers, pairing or jobs."""
    if isinstance(value, dict):
        return {k: filtered(v) for k, v in value.items() if not PRIVATE_FIELD.search(k) and (
            k != 'sandbox_env' or isinstance(v, dict) and set(v) <= {'file', 'keys'})}
    if isinstance(value, list):
        return [filtered(v) for v in value]
    return value


def rewrite(text, mapping):
    # One substitution pass: a remote path may itself contain a local prefix.
    pairs = {p.rstrip('/'): v for p, v in sorted(mapping, key=lambda row: len(row[0]), reverse=True)}
    pattern = '|'.join(re.escape(p.rstrip('/')) for p in pairs)
    return re.sub('(?:' + pattern + r')(?=$|[/\s\"\'\\*])', lambda m: pairs[m[0]].rstrip('/'), text)


def snapshot(record, status):
    """Return {path: (bytes, mode)}, scopes, exclusions. Scan the exact bytes sent later."""
    home = Path(common.REAL_HOME)
    remote = status['home'].rstrip('/')
    mapping = [(str(home), remote), (str(Path(common.PIPER_HOME)), status['state']), ('~', remote), ('$HOME', remote)]
    local_root = os.environ.get('PROJECT_PATH', '')
    if local_root:
        mapping.append((local_root, status['requested_project_root']))
    mapping += [(p['local_path'], p['remote_path']) for p in record['path_map']]
    files, scopes, left = {}, [], []
    total = 0

    def omit(path, reason):
        left.append(dict(path=path, reason=reason))
        if len(left) > MAX_FILES:
            raise ValueError('Configuration has too many excluded files.')

    def add(path, raw, mode=0o600, soft=False):
        nonlocal total
        if soft and (total + len(raw) > MAX_TOTAL or len(files) >= MAX_FILES):
            return False
        if len(raw) > MAX_FILE:
            omit(path, 'over 50 MB'); return
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            # Scan bytes through a reversible single-byte decoding too (archives may contain
            # documents/images). ASCII secret signatures remain visible, including binary files.
            text = raw.decode('latin-1')
        findings = common.secret_findings(text)
        if findings:
            omit(path, 'secret scan: ' + ', '.join(findings)); return
        if path.lower().endswith('.env.json'):
            try:
                def has_value(v):
                    return any(has_value(x) for x in v.values()) if isinstance(v, dict) else (
                        any(has_value(x) for x in v) if isinstance(v, list) else v not in ('', None))
                if has_value(json.loads(text)):
                    omit(path, 'environment JSON contains values'); return
            except ValueError:
                omit(path, 'unreadable environment JSON'); return
        files[path] = (raw, mode)
        total += len(raw)
        if total > MAX_TOTAL or len(files) > MAX_FILES:
            raise ValueError('Configuration snapshot exceeds the setup size limit.')

    def tree(source, target, *, is_tree=True, transform=False, read_root=None, dev=False):
        source = Path(source)
        skipped = 0
        boundary = Path(read_root or home)
        scopes.append(dict(path=target, tree=is_tree))
        # No symlink traversal. Reject hard links as well: a harmless name can alias a login.
        def visit(path, dest):
            nonlocal skipped
            reason = excluded(path.name)
            if reason:
                omit(dest, reason); return
            if path.is_symlink():
                omit(dest, 'symlink'); return
            if not path.exists():
                return
            st = path.lstat()
            if dev and path.is_dir() and path.name in DEV_SKIP_DIRS:
                skipped += 1; return
            if dev and path.is_file() and (not dev_text(path.name) or st.st_size > DEV_FILE):
                skipped += 1; return
            if path.is_dir():
                for child in sorted(path.iterdir()):
                    visit(child, dest + '/' + child.name)
            elif path.is_file() and st.st_nlink == 1:
                if st.st_size > MAX_FILE:
                    omit(dest, 'over 50 MB'); return
                try:
                    raw = common.safe_read(str(boundary), str(path.relative_to(boundary)), MAX_FILE + 1,
                                           single_link=True)
                    if transform:
                        raw = rewrite(raw.decode('utf-8'), mapping).encode()
                except (OSError, ValueError, UnicodeError):
                    omit(dest, 'unreadable file')
                else:
                    if add(dest, raw, 0o700 if st.st_mode & 0o111 else 0o600, soft=dev) is False:
                        skipped += 1
            else:
                omit(dest, 'non-regular or hard-linked file')
        visit(source, target)
        if skipped:
            # A summary, not a protected path: it never blocks copying the text files beside it.
            left.append(dict(path=target, reason='%d files or folders not copied: development/ mirrors text files up to '
                             '1 MB within the setup size limit' % skipped, summary=True))

    for agent, seed in (('.claude', sessioncfg.SEED), ('.codex', codexcfg.SEED)):
        names = list(seed)
        if agent == '.claude':
            names += [n for n in sessioncfg.referenced_files(str(home / agent / 'settings.json'), str(home / agent))
                      if not any(excluded(p) or p.lower() in sessioncfg.NEVER_LOWER for p in Path(n).parts)]
        dirs = {'skills', 'commands', 'agents', 'output-styles', 'hooks', 'rules', 'prompts'}
        for name in dict.fromkeys(names):
            dest = remote + '/' + agent + '/' + name
            scopes.append(dict(path=dest, tree=name in dirs))
            refused = []
            content = sessioncfg._tree(str(home / agent / name),
                {str(home / agent): {n.split('/')[0] for n in names}, str(home / '.agents'): None},
                deny=[str(home), common.PIPER_HOME] + ([local_root] if local_root else []),
                secrets=[str(home / agent / ('.credentials.json' if agent == '.claude' else 'auth.json'))],
                refused=refused)
            for rel in refused:
                omit(dest + ('/' + rel if rel else ''), 'unsafe or unreadable seed file')
            for rel, (raw, mode) in content.items():
                target = dest + ('/' + rel if rel else '')
                if any(excluded(p) for p in Path(name, rel).parts):
                    omit(target, 'secret or unsafe name'); continue
                try:
                    raw = rewrite(raw.decode('utf-8'), mapping).encode()
                except UnicodeError:
                    omit(target, 'unreadable seed file'); continue
                add(target, raw, 0o700 if mode & 0o111 else 0o600)
    for p in record['path_map']:
        key = lambda path: re.sub(r'[^A-Za-z0-9]', '-', path)
        tree(home / '.claude/projects' / key(p['local_path']) / 'memory',
             remote + '/.claude/projects/' + key(p['remote_path']) + '/memory', transform=True)
    for name in ('policy.json', 'projects.json'):
        target = status['state'].rstrip('/') + '/' + name
        scopes.append(dict(path=target, tree=False))
        source = Path(common.PIPER_HOME) / name
        if source.exists():
            raw = common.safe_read(str(source.parent), name, MAX_FILE + 1, single_link=True)
            if len(raw) > MAX_FILE:
                raise ValueError('Configuration JSON exceeds the setup size limit.')
            data = filtered(json.loads(raw))
            if not isinstance(data, dict):
                raise ValueError('Configuration JSON must be an object.')
            add(target, rewrite(json.dumps(data, ensure_ascii=False, sort_keys=True), mapping).encode())
    # development/ last: it takes only what the required configuration above leaves of the budget.
    for p in record['path_map']:
        tree(Path(p['local_path']) / 'development', p['remote_path'] + '/development', read_root=p['local_path'], dev=True)
    return files, sorted(scopes, key=lambda s: s['path']), sorted(left, key=lambda s: s['path'])


def hashes(files):
    return {path: dict(sha256=hashlib.sha256(raw).hexdigest(), mode=mode) for path, (raw, mode) in sorted(files.items())}


def archive(files, paths):
    """Numbered regular members: destinations are in the separately confirmed hash manifest."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as tar:
        for n, path in enumerate(sorted(paths)):
            raw, mode = files[path]
            info = tarfile.TarInfo(str(n))
            info.size, info.mode = len(raw), mode
            tar.addfile(info, io.BytesIO(raw))
    raw = stream.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def local_secret(record, row):
    """The value of a server .env key in the same file on this computer, or None. Never logged or stored.

    Only .env files map unambiguously: other templates hold placeholders, the local file holds the value
    somewhere else. The server path maps back through the project path map or the account home.
    """
    file = row['file']
    if not (file.endswith('/.env') or file.endswith('.env')):
        return None
    if file.startswith('~/'):
        root, rel = Path(common.REAL_HOME), file[2:]
    else:
        match = next((p for p in sorted(record['path_map'], key=lambda p: len(p['remote_path']), reverse=True)
                      if file.startswith(p['remote_path'].rstrip('/') + '/')), None)
        if not match:
            return None
        root, rel = Path(match['local_path']), file[len(match['remote_path'].rstrip('/')) + 1:]
    try:
        raw = common.safe_read(str(root), rel, 1 << 20, single_link=True).decode('utf-8')
    except (OSError, ValueError, UnicodeError):
        return None
    value = None
    for line in raw.splitlines():
        m = ENV_KEY.match(line)
        if not m or m[1] != row['key']:
            continue
        text = line[m.end():].strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in '"\'':
            text = text[1:-1]
        else:
            text = re.split(r'\s+#', text, maxsplit=1)[0].strip()
        value = text
    if not value or len(value) > 16384 or any(ord(c) < 32 for c in value):
        return None
    return value


def secret_rows(record, requirements):
    """Metadata only: template defaults are never sent, returned, or saved."""
    rows = [dict(s, source=r['id']) for r in requirements for s in r['secrets']]
    for p in record['path_map']:
        root = Path(p['local_path'])
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {'.git', '.claude', 'node_modules', 'target', '.venv'}
                             and not Path(directory, d).is_symlink())
            if '.env.example' not in names:
                continue
            template = Path(directory) / '.env.example'
            try:
                raw = common.safe_read(str(root), template.relative_to(root).as_posix(), 1 << 20, single_link=True)
                keys = sorted({m[1] for line in raw.decode('utf-8').splitlines() if (m := ENV_KEY.match(line))})
            except (OSError, ValueError, UnicodeError):
                continue
            target = p['remote_path'].rstrip('/') + '/' + (template.parent.relative_to(root) / '.env').as_posix()
            rows += [dict(file=target, key=key, label=key, source=p['project']) for key in keys]
    result = {}
    for row in rows:
        file = row['file']
        if not file.startswith('/'):
            file = '~/' + file.removeprefix('~/')
        sid = hashlib.sha256((file + '\0' + row['key']).encode()).hexdigest()[:24]
        result[sid] = dict(row, file=file, id=sid)
    if len(result) > 1000:
        raise ValueError('Too many server secret names.')
    return list(result.values())
