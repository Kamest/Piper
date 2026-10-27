"""Per-run bounded RPC inside the existing HTTP jail, never a host file capability.

The child can submit arbitrary messages. Only the host callback decides what an
approved project dependency means; replies are written through pinned directory FDs.
No child-directed host destination, shell, socket or runtime configuration is accepted.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time

MAX_MESSAGE = 64 << 10
MAX_REQUESTS = 65536
MAX_TOTAL = 64 << 20


class Broker:
    def __init__(self, work: str, callback, timeout: float):
        self.path = os.path.join(work, 'runtime', 'broker')
        os.makedirs(self.path, mode=0o700)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.callback = callback
        self.total = 0
        self.deadline = time.monotonic() + timeout
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='piper-http-files', daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        # A callback must obey the remaining run deadline. Keep its FD alive until
        # it returns, including when a native process failed while awaiting a card.
        self.thread.join(max(0, self.deadline - time.monotonic()) + 1)
        if self.thread.is_alive():
            raise ValueError('HTTP runtime broker did not stop; requests may already have run; not retried')
        os.close(self.fd)

    def _read(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=self.fd)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_MESSAGE:
                raise ValueError('invalid HTTP runtime message file')
            data = stream.read(MAX_MESSAGE + 1)
        if len(data) > MAX_MESSAGE:
            raise ValueError('HTTP runtime message exceeds 64 KiB')
        self.total += len(data)
        if self.total > MAX_TOTAL:
            raise ValueError('HTTP runtime aggregate message limit')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('invalid HTTP runtime message')
        return value

    def _reply(self, number, result):
        data = json.dumps(result, ensure_ascii=True).encode()
        self.total += len(data)
        if self.total > MAX_TOTAL:
            data = b'{"ok":false,"error":"HTTP runtime aggregate message limit"}'
        if len(data) > MAX_MESSAGE:
            data = b'{"ok":false,"error":"HTTP runtime reply exceeds 64 KiB"}'
        name = 'reply-%d.json' % number
        # A hostile child can precreate the name, but never redirect a host write.
        fd = os.open(name + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.rename(name + '.tmp', name, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def _run(self):
        for number in range(MAX_REQUESTS):
            name = 'request-%d.json' % number
            while not self.stop.is_set() and time.monotonic() < self.deadline:
                try:
                    message = self._read(name)
                except FileNotFoundError:
                    self.stop.wait(.02)
                    continue
                except (OSError, ValueError):
                    # Never include attacker-supplied payloads/paths in diagnostics.
                    result = dict(ok=False, error='invalid HTTP runtime message')
                else:
                    try:
                        result = dict(ok=True, **self.callback(message, self.deadline))
                    except (OSError, ValueError):
                        result = dict(ok=False, error='HTTP runtime dependency was refused; requests may already have run; not retried')
                try:
                    self._reply(number, result)
                    os.unlink(name, dir_fd=self.fd)
                except OSError:
                    return
                if self.total > MAX_TOTAL:
                    return
                break
            else:
                return


class Inputs:
    """Runtime body dependencies stay project-bound, visible and snapshotted before use."""
    def __init__(self, root, work, hidden, approve=None, forbidden=None, max_bytes=50 << 20, generated=None):
        self.root, self.work = os.path.realpath(root), work
        self.hidden, self.approve, self.forbidden = hidden, approve, forbidden
        self.max_bytes, self.total = max_bytes, 0
        self.generated = generated
        self.references = {}
        self.sequence = 0
        self.failure = None
        self.cache = {}
        self.directory = os.path.join(work, 'runtime', 'inputs')
        os.makedirs(self.directory, mode=0o700)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        os.close(self.fd)

    def add(self, owner, reference, origin=None, index=None):
        import secrets
        token = secrets.token_hex(16)
        self.references[token] = dict(owner=owner, origin=origin or {}, index=index)
        return 'piper-runtime:' + token + ':' + reference

    def __call__(self, message, deadline):
        try:
            return self._resolve(message, deadline)
        except (OSError, ValueError):
            reference = message.get('reference') if isinstance(message, dict) else None
            token = reference.partition(':')[0] if isinstance(reference, str) else ''
            self.failure = self.references.get(token)
            raise

    def _resolve(self, message, deadline):
        import hashlib
        import re
        import common
        import guard
        if (set(message) not in ({'kind', 'reference'}, {'kind', 'reference', 'iteration'}) or message['kind'] != 'file' or
                not isinstance(message['reference'], str) or message.get('iteration') is not None and
                (type(message['iteration']) is not int or not 0 <= message['iteration'] < 2048)):
            raise ValueError('invalid HTTP runtime input capability')
        token, separator, reference = message['reference'].partition(':')
        if not separator or token not in self.references or not reference or len(reference) > 4096:
            raise ValueError('unknown HTTP runtime input capability')
        if (reference.startswith('~') or '\\' in reference or '{{' in reference or
                any(ord(c) < 32 for c in reference) or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', reference)):
            raise ValueError('invalid HTTP runtime input path')
        descriptor = dict(self.references[token], iteration=message.get('iteration'))
        owner = descriptor['owner']
        path = os.path.normpath(reference if os.path.isabs(reference) else
                                os.path.join(self.root, os.path.dirname(owner), reference))
        relative = os.path.relpath(path, self.root).replace(os.sep, '/')
        if relative == '..' or relative.startswith('../') or guard.is_git_path(relative) or self.hidden(relative):
            raise ValueError('HTTP runtime input is hidden or outside the project')
        produced = self.generated(relative, descriptor, deadline) if self.generated else None
        data = produced['data'] if produced is not None else None
        cache_key = (token, relative, produced['stage'], hashlib.sha256(data).hexdigest()) if produced is not None else (token, relative)
        if cache_key in self.cache:
            return dict(self.cache[cache_key])
        if data is None:
            data = common.safe_read(self.root, relative, self.max_bytes + 1, single_link=True)
        if len(data) > self.max_bytes or self.total + len(data) > 128 << 20:
            raise ValueError('HTTP runtime input size limit')
        if self.forbidden and self.forbidden.search(data.decode('utf-8', 'replace')):
            raise ValueError('HTTP runtime input contains unavailable host variables')
        digest = hashlib.sha256(data).hexdigest()
        if self.approve:
            self.approve(dict(path=relative, size=len(data), sha=digest, data=data, origin=descriptor['origin'],
                              generated=produced is not None), deadline)
        if time.monotonic() >= deadline:
            raise ValueError('HTTP runtime input deadline')
        extension = os.path.splitext(relative)[1]
        if not re.fullmatch(r'\.[A-Za-z0-9]{1,10}', extension):
            extension = '.bin'
        name = '%d-%s%s' % (self.sequence, digest, extension)
        self.sequence += 1
        # Pin the output directory too: native code cannot redirect a host write.
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=self.fd)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        self.total += len(data)
        self.cache[cache_key] = dict(path=os.path.join(self.directory, name))
        return dict(self.cache[cache_key])


class Generated:
    """Resolve completed private exports in native order, without publishing or replaying.

    Only bounded sequential stage names created for the approved request graph
    are read. All destination validation and overwrite naming remain with the host.
    """
    def __init__(self, root, work, redirects, target, name, max_bytes=50 << 20):
        self.root, self.work, self.redirects = root, work, redirects
        self.target, self.name, self.max_bytes = target, name, max_bytes

    def __call__(self, relative, descriptor, deadline):
        import common
        reserved, selected, size, count = set(), None, 0, 0
        for directive in self.redirects:
            if directive['request'] > descriptor['index']:
                continue
            for iteration in range(2048):
                if time.monotonic() >= deadline:
                    raise ValueError('HTTP generated input deadline')
                stage = directive['staged'] + ('-' + str(iteration) if iteration else '')
                try:
                    raw = common.safe_read(self.work, os.path.relpath(stage + '.target.json', self.work), 32 << 10, single_link=True)
                except FileNotFoundError:
                    break
                size += len(raw)
                count += 1
                if size > 4 << 20 or count > 2048:
                    raise ValueError('HTTP generated input metadata limit')
                metadata = json.loads(raw)
                if not isinstance(metadata, dict) or set(metadata) != {'path'} or not isinstance(metadata['path'], str):
                    raise ValueError('invalid HTTP generated input metadata')
                try:
                    info = common.safe_stat(self.work, os.path.relpath(stage, self.work))
                except FileNotFoundError:
                    continue  # A native converter can exist before its HTTP response.
                if info.st_nlink != 1 or info.st_size > self.max_bytes:
                    raise ValueError('invalid HTTP generated response stage')
                path = self.target(directive, metadata['path'], iteration)
                path = self.name(self.root, path, directive['overwrite'], reserved)
                reserved.add(path)
                if path == relative:
                    selected = stage
        if selected is None:
            return None
        data = common.safe_read(self.work, os.path.relpath(selected, self.work), self.max_bytes + 1, single_link=True)
        return dict(data=data, stage=selected)


def preview(data: bytes, scrub) -> dict:
    """Mask complete values before clipping a small UTF-8 text preview for the human."""
    try:
        text = data.decode('utf-8')
        if '\0' in text:
            raise ValueError('binary')
    except (UnicodeDecodeError, ValueError):
        return dict(preview=None, preview_note='', binary='application/octet-stream')
    text = scrub(text)
    lines = text.splitlines()
    shown = '\n'.join(line[:160] for line in lines[:12]).encode()[:2048].decode('utf-8', 'ignore')
    note = 'bounded text preview · %d of %d lines · %d of %d bytes' % (
        min(12, len(lines)), len(lines), len(shown.encode()), len(data))
    return dict(preview=shown, preview_note=note, binary=None)
