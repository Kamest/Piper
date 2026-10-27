"""Read immutable, project-bounded dependency graphs for native HTTP script modules.

The lexer discovers resource references, not executable behavior. The native JS engine
still parses/evaluates the original module sources; no user code runs during preflight.
"""
from __future__ import annotations

import os
import re

import common

MAX_FILES = 2048
MAX_BYTES = 64 << 20
MAX_CODE_CACHE_BYTES = 128 << 20


class Discovery:
    """Host-owned queue for a parse-only native JVM. Reads remain project bounded.

    The parser receives private immutable paths, never a project root or credentials.
    Its output is untrusted metadata and cannot select a host output destination.
    """
    def __init__(self, root, work, entries, sources, hidden, max_files=MAX_FILES, max_bytes=MAX_BYTES, read=None):
        self.root, self.work, self.hidden = root, work, hidden
        self.max_files, self.max_bytes = max_files, max_bytes
        self.read = read or (lambda path, limit:common.safe_read(self.root, path, limit, single_link=True))
        self.modules, self.parsed = {}, {}
        self.pending = [(owner, source, False) for owner, source in sources] + [(entry, None, True) for entry in entries]
        self.current, self.index, self.size, self.failure = None, 0, 0, None
        os.makedirs(os.path.join(work, 'project'), mode=0o700)

    def __call__(self, message, deadline):
        import time
        try:
            if (set(message) != {'kind', 'index'} or message['kind'] != 'parser_next' or
                    type(message['index']) is not int or message['index'] != self.index or time.monotonic() >= deadline):
                raise ValueError('invalid native module parser sequence')
            if self.current is not None:
                import json
                owner, source = self.current
                raw = common.safe_read(self.work, 'runtime/tmp/parse-%d.json' % (self.index - 1), MAX_BYTES + 1, single_link=True)
                if len(raw) > MAX_BYTES:
                    raise ValueError('native module parser metadata exceeds its limit')
                metadata = json.loads(raw)
                if (not isinstance(metadata, dict) or set(metadata) - {'refs', 'computed', 'error_line'} or
                        not isinstance(metadata.get('refs'), list) or not isinstance(metadata.get('computed'), list)):
                    raise ValueError('invalid native module parser metadata')
                if 'error_line' in metadata:
                    # No partial dependency graph is ever approved or executed.
                    raise ValueError('native JavaScript syntax error in %s:%s; no code was evaluated' %
                                     (owner, metadata['error_line'] if type(metadata['error_line']) is int else '?'))
                refs = []
                for item in metadata['refs']:
                    if (not isinstance(item, dict) or set(item) != {'ref', 'start', 'end'} or
                            not isinstance(item['ref'], str) or type(item['start']) is not int or type(item['end']) is not int or
                            not 0 <= item['start'] < item['end'] <= len(source.encode('utf-16-le')) // 2):
                        raise ValueError('invalid native module reference')
                    refs.append(item)
                self.parsed[source] = refs
                self.pending.extend((module_path(owner, item['ref']), None, True) for item in refs)
            while self.pending:
                owner, source, is_module = self.pending.pop(0)
                if is_module:
                    if owner in self.modules:
                        continue
                    if self.hidden(owner):
                        raise ValueError('%s is hidden by the isolation policy' % owner)
                    try:
                        try:
                            data = self.read(owner, self.max_bytes - self.size + 1)
                        except FileNotFoundError:
                            if os.path.splitext(owner)[1]:
                                raise
                            owner += '.js'
                            if self.hidden(owner):
                                raise ValueError('%s is hidden by the isolation policy' % owner)
                            if owner in self.modules:
                                continue
                            data = self.read(owner, self.max_bytes - self.size + 1)
                    except (OSError, ValueError):
                        raise ValueError('cannot safely read JS module %s inside the project' % owner) from None
                    if data is None:
                        continue  # A declared response export may create this dependency later.
                    if len(self.modules) >= self.max_files:
                        raise ValueError('JS dependency count exceeds the configured limit')
                    self.size += len(data)
                    if self.size > self.max_bytes:
                        raise ValueError('JS dependency size exceeds the configured limit')
                    try:
                        source = data.decode('utf-8-sig')
                    except UnicodeDecodeError:
                        raise ValueError('JS modules must contain UTF-8 source') from None
                    if '\0' in source:
                        raise ValueError('JS modules must contain UTF-8 text without NUL')
                    self.modules[owner] = data
                # Reuse parse results, but still discover imports relative to this owner.
                if source in self.parsed:
                    self.pending.extend((module_path(owner, item['ref']), None, True) for item in self.parsed[source])
                    continue
                name = 'source-%d.js' % self.index
                path = os.path.join(self.work, 'project', name)
                with open(path, 'xb') as stream:
                    stream.write(source.encode('utf-8'))
                os.chmod(path, 0o400)
                self.current = owner, source
                self.index += 1
                return dict(done=False, name=owner, path=path)
            self.current = None
            return dict(done=True)
        except (OSError, ValueError) as error:
            self.failure = error
            raise


def native_graph(binary, env, directory, root, entries, sources, hidden, timeout=180, read=None):
    """Use the installed Graal parser in the existing OS jail, with no permitted egress.

    This process only parses immutable copies. It cannot execute the HTTP file or
    replay a pre-handler. The later request process remains the sole executor.
    """
    import tempfile
    import httpbroker
    import httpjail
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='module-parse-', dir=directory) as work:
        discovery = Discovery(root, work, entries, sources, hidden, read=read)
        proxy = httpjail.Egress([], timeout, unix=__import__('sys').platform.startswith('linux'))
        try:
            with httpbroker.Broker(work, discovery, timeout):
                result = httpjail.run(binary, work, ['--piper-parse-modules'], env, timeout, proxy.port,
                                      proxy_path=proxy.unix_path)
        finally:
            proxy.close()
        if discovery.failure:
            raise ValueError(str(discovery.failure)) from None
        if result.returncode or discovery.current is not None or discovery.pending:
            raise ValueError('native JavaScript dependency parser did not complete; no code was evaluated')
        return discovery.modules, discovery.parsed


class Code:
    """One-run executable snapshots. A data-file approval never permits execution."""
    def __init__(self, binary, env, root, work, modules, hidden, approve, forbidden=None, generated=None, source_maps=None):
        self.binary, self.env, self.root, self.work = binary, env, root, work
        self.approved, self.hidden, self.approve = dict(modules), hidden, approve
        self.forbidden, self.generated = forbidden, generated
        self.contexts, self.snapshots, self.failure = {}, {}, None
        self.written, self.written_bytes = {}, 0
        self.roots = {}
        self.source_maps = source_maps if source_maps is not None else {}
        os.makedirs(os.path.join(work, 'runtime'), mode=0o700, exist_ok=True)
        os.mkdir(os.path.join(work, 'code'), 0o700)
        os.mkdir(os.path.join(work, 'code-roots'), 0o700)
        self.fd = os.open(os.path.join(work, 'code'), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        os.close(self.fd)

    def add(self, descriptor):
        import secrets
        token = secrets.token_hex(16)
        self.contexts[token] = dict(descriptor)
        return '/*piper-code:%s*/' % token

    def configure(self, files):
        import hashlib
        import json
        known = {}
        for path in files:
            relative = os.path.relpath(path, self.work)
            data = common.safe_read(self.work, relative, MAX_BYTES + 1, single_link=True)
            if len(data) > MAX_BYTES:
                raise ValueError('native script snapshot exceeds its limit')
            known[path] = hashlib.sha256(data).hexdigest()
        with open(os.path.join(self.work, 'code-state.json'), 'x') as stream:
            json.dump(dict(contexts=self.contexts, files=known), stream)
        os.chmod(os.path.join(self.work, 'code-state.json'), 0o400)

    def __call__(self, message, deadline):
        import hashlib
        import time
        import guard
        token = message.get('token')
        descriptor = self.contexts.get(token) if isinstance(token, str) else None
        try:
            if message.get('kind') == 'code_abort':
                if (set(message) != {'kind', 'token', 'step'} or descriptor is None or
                        message.get('step') not in ('substitution', 'approval', 'snapshot', 'source_file')):
                    raise ValueError('invalid runtime code abort')
                descriptor = dict(descriptor, reason='runtime script stopped during ' + message['step'])
                raise ValueError(descriptor['reason'])
            script = message.get('kind') == 'script_load'
            if script:
                reference = message.get('reference')
                if (set(message) != {'kind', 'token', 'reference', 'iteration', 'prefix'} or descriptor is None or
                        'script_reference' not in descriptor or not isinstance(reference, str) or not reference or
                        type(message.get('prefix')) is not int or not 0 <= message['prefix'] <= 1 << 20 or
                        len(reference) > 4096 or reference.startswith('~') or '{{' in reference or '\\' in reference or
                        any(ord(char) < 32 for char in reference) or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', reference)):
                    raise ValueError('invalid runtime script reference')
                prefix = message['prefix']
                resolved = os.path.normpath(reference if os.path.isabs(reference) else
                    os.path.join(self.root, os.path.dirname(descriptor['owner']), reference))
                message = dict(kind='code_load', token=token, path=os.path.relpath(resolved, self.root).replace(os.sep, '/'),
                               iteration=message['iteration'])
            if (set(message) not in ({'kind', 'token', 'path'}, {'kind', 'token', 'path', 'iteration'}) or message['kind'] != 'code_load' or descriptor is None or
                    not isinstance(message['path'], str) or len(message['path']) > 4096 or message.get('iteration') is not None and
                    (type(message['iteration']) is not int or not 0 <= message['iteration'] < MAX_FILES)):
                raise ValueError('invalid HTTP runtime code capability')
            descriptor = dict(descriptor, iteration=message.get('iteration'), code=True)
            path = module_path('root.http', message['path'])
            def hidden(relative):
                return (guard.is_git_path(relative) or os.path.basename(relative) == 'http-client.private.env.json' or
                        self.hidden(relative))
            if hidden(path):
                raise ValueError('runtime code is hidden or private; not shown or run')
            produced = self.generated(path, descriptor, deadline) if self.generated else None
            different = produced is not None and produced['data'] != self.approved.get(path)
            if path not in self.approved or different:
                downloaded = set()
                def read(relative, limit):
                    if self.generated:
                        produced = self.generated(relative, descriptor, deadline)
                        if produced is not None:
                            downloaded.add(relative)
                            return produced['data']
                    if relative in self.approved:
                        return self.approved[relative]
                    return common.safe_read(self.root, relative, limit, single_link=True)
                remaining = deadline - time.monotonic()
                if remaining <= 5:
                    raise ValueError('runtime code not asked: less than 5 s of the run deadline left')
                graph, _ = native_graph(self.binary, self.env, os.path.join(self.work, 'runtime', 'parse'), self.root,
                    [path], [], hidden, max(1, int(remaining - 5)), read=read)
                if path not in graph and path + '.js' in graph:
                    path += '.js'
                combined = self.approved | graph
                # Discovery enforces the whole per-card graph limit. Do not
                # subtract an earlier graph from a later card's allowance. Bound
                # the run cache separately, including empty-file/path metadata.
                cache_bytes = sum(len(data) + 4 * len(relative) + 1024 for relative, data in combined.items())
                if cache_bytes > MAX_CODE_CACHE_BYTES:
                    raise ValueError('runtime approved-code cache exceeds %d MiB including paths and metadata' % (MAX_CODE_CACHE_BYTES >> 20))
                if self.forbidden and any(self.forbidden.search(data.decode('utf-8-sig')) for data in graph.values()):
                    raise ValueError('runtime code contains unavailable host variables')
                details = [dict(path=relative, size=len(data), sha=hashlib.sha256(data).hexdigest()) for relative, data in sorted(graph.items())]
                if self.approve is None:
                    raise ValueError('runtime code requires a concrete one-shot approval')
                if any(self.approved.get(relative) != data for relative, data in graph.items()):
                    self.approve(dict(path=path, size=len(graph[path]), sha=hashlib.sha256(graph[path]).hexdigest(),
                        data=graph[path], deps=[item for item in details if item['path'] != path],
                        files=len(graph), bytes=sum(map(len, graph.values())),
                        **dict(descriptor, trigger='downloaded' if downloaded else descriptor['trigger'])), deadline)
                if time.monotonic() >= deadline:
                    raise ValueError('runtime code approval deadline')
                for relative, data in graph.items():
                    if self.approved.get(relative) != data:
                        self.snapshots.pop(relative, None)
                self.approved.update(graph)
            if path not in self.snapshots:
                data = self.approved[path]
                digest = hashlib.sha256(data).hexdigest()
                if digest not in self.written:
                    if self.written_bytes + len(data) > 2 * MAX_BYTES:
                        raise ValueError('runtime code snapshot aggregate exceeds 128 MiB')
                    name = digest + '.js'
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=self.fd)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                    self.written[digest] = os.path.join(self.work, 'code', name)
                    self.written_bytes += len(data)
                self.snapshots[path] = dict(path=self.written[digest], sha=digest)
                origins = [(path, index) for index, _ in enumerate(re.split(r'\r\n|[\r\n\u2028\u2029]', data.decode('utf-8-sig')))]
                self.source_maps[self.written[digest]] = origins
                self.source_maps[os.path.join(self.work, 'project', path)] = origins
            if script:
                key = (token, path, self.snapshots[path]['sha'])
                if key not in self.roots:
                    directory = os.path.join(self.work, 'code-roots', str(len(self.roots)))
                    target = os.path.join(directory, path)
                    os.makedirs(os.path.dirname(target), mode=0o700)
                    # No path comes from the child without the project/private/hidden checks above.
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(self.approved[path])
                    self.roots[key] = dict(path=target, root=directory, entry=path, sha=self.snapshots[path]['sha'])
                self.source_maps[self.roots[key]['path']] = [None] * prefix + self.source_maps[self.snapshots[path]['path']]
                return self.roots[key]
            return self.snapshots[path]
        except (OSError, ValueError) as error:
            if descriptor is not None and message.get('kind') != 'code_abort':
                descriptor = dict(descriptor, reason='runtime code dependency was refused')
            self.failure = descriptor
            raise


def _quoted(source: str, start: int) -> tuple[str, int]:
    quote, out, i = source[start], [], start + 1
    escapes = {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '0': '\0'}
    while i < len(source):
        char = source[i]
        i += 1
        if char == quote:
            return ''.join(out), i
        if char in '\r\n':
            raise ValueError('unterminated JS module string')
        if char != '\\':
            out.append(char)
            continue
        if i >= len(source):
            break
        escape = source[i]
        i += 1
        if escape in '\r\n':
            if escape == '\r' and source[i:i + 1] == '\n':
                i += 1
        elif escape in ('u', 'x'):
            width = 4 if escape == 'u' else 2
            if escape == 'u' and source[i:i + 1] == '{':
                end = source.find('}', i + 1)
                if end < 0:
                    raise ValueError('invalid Unicode escape in JS module string')
                digits, i = source[i + 1:end], end + 1
            else:
                digits, i = source[i:i + width], i + width
                if len(digits) != width:
                    raise ValueError('invalid escape in JS module string')
            try:
                out.append(chr(int(digits, 16)))
            except (ValueError, OverflowError):
                raise ValueError('invalid escape in JS module string') from None
        else:
            out.append(escapes.get(escape, escape))
    raise ValueError('unterminated JS module string')


def _tokens(source: str, positions: dict | None = None) -> list[tuple[str, str]]:
    tokens = []
    regex_after = {'=', '(', '[', '{', ':', ',', ';', '!', '?', '=>', '+', '-', '*', '/', '%',
                   '&', '|', '^', '~', '<', '>', 'return', 'yield', 'case', 'throw', 'else', 'do',
                   'delete', 'typeof', 'void', 'new', 'in', 'instanceof', 'await', 'break', 'continue', 'debugger'}

    def regex_start() -> bool:
        if not tokens:
            return True
        kind, value = tokens[-1]
        if kind == 'punctuation':
            return value in regex_after
        return (kind == 'word' and value in regex_after and
                not (len(tokens) > 1 and tokens[-2][1] in ('.', '#')))

    def scan(i: int, expression: bool = False) -> int:
        depth = 0
        parens = []
        while i < len(source):
            char = source[i]
            if char.isspace() or char == '\ufeff':
                i += 1
            elif source.startswith('//', i):
                end = re.search(r'[\r\n\u2028\u2029]', source[i + 2:])
                i = len(source) if end is None else i + 3 + end.start()
            elif source.startswith('/*', i):
                end = source.find('*/', i + 2)
                if end < 0:
                    raise ValueError('unterminated JS comment')
                i = end + 2
            elif char in ('"', "'"):
                start = i
                value, i = _quoted(source, i)
                if positions is not None:
                    positions[len(tokens)] = (start, i)
                tokens.append(('string', value))
            elif char == '`':
                tokens.append(('template', '`'))
                i += 1
                while i < len(source):
                    if source[i] == '\\':
                        i += 2
                    elif source.startswith('${', i):
                        i = scan(i + 2, True)
                    elif source[i] == '`':
                        i += 1
                        break
                    else:
                        i += 1
                tokens.append(('template', '`'))
            elif char == '/' and tokens and tokens[-1][1] in ('}', 'of'):
                # Block/object and contextual `of` need a complete JS grammar. Never
                # guess and accidentally hide an import inside a misread string.
                raise ValueError('ambiguous JS slash needs a complete module parser')
            elif char == '/' and regex_start():
                # Regex strings may themselves contain import-looking text and quoted paths.
                i, bracket = i + 1, False
                while i < len(source):
                    current = source[i]
                    i += 1
                    if current == '\\':
                        i += 1
                    elif current == '[':
                        if bracket:
                            raise ValueError('ambiguous nested JS regex class needs a complete module parser')
                        bracket = True
                    elif current == ']':
                        bracket = False
                    elif current == '/' and not bracket:
                        break
                flags = i
                while i < len(source) and source[i].isalpha():
                    i += 1
                if 'v' in source[flags:i]:
                    raise ValueError('ambiguous JS regex v flag needs a complete module parser')
                tokens.append(('regex', '/'))
            elif char.isalpha() or char in ('_', '$'):
                end = i + 1
                while end < len(source) and (source[end].isalnum() or source[end] in ('_', '$')):
                    end += 1
                tokens.append(('word', source[i:end]))
                i = end
            else:
                if char == '}' and expression and depth == 0:
                    return i + 1
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                if char == '(':
                    control = bool(tokens and tokens[-1][0] == 'word' and
                                   (tokens[-1][1] in ('if', 'while', 'for', 'with', 'switch', 'catch') or
                                    tokens[-2:] == [('word', 'for'), ('word', 'await')]))
                    parens.append(control)
                control = char == ')' and parens and parens.pop()
                pair = source[i:i + 2]
                token = pair if pair in ('=>', '++', '--', '/=') else char
                tokens.append(('punctuation', ';' if control else token))
                i += len(token)
        return i

    scan(0)
    return tokens


def module_dependencies(source: str) -> list[str]:
    """Literal import/re-export paths, with comments, strings and regex contents ignored.

    A computed module path cannot be approved before execution. Refuse it explicitly
    instead of letting the host engine fetch an unexamined file after an HTTP write.
    """
    # No module keywords means no static dependencies. Leave the rest of valid
    # JavaScript grammar to the native engine (regex v/classes, object division).
    # Dynamic code remains confined by the OS and can only load the snapshot.
    if not re.search(r'\b(?:import|export)\b', source):
        return []
    tokens = _tokens(source)
    return [tokens[index][1] for index in _module_references(tokens)]


def _module_references(tokens: list) -> list[int]:
    refs = []
    for i, (kind, value) in enumerate(tokens):
        if (kind != 'word' or value not in ('import', 'export') or i + 1 == len(tokens)
                or (i and tokens[i - 1][1] in ('.', '#'))):
            continue
        following = tokens[i + 1]
        if value == 'import' and following[1] == '.':
            continue  # import.meta has no dependency
        if value == 'import' and following[1] == '(':
            if i + 3 >= len(tokens) or tokens[i + 2][0] != 'string' or tokens[i + 3][1] not in (')', ','):
                raise ValueError('a dynamic JS import must use a literal project path')
            refs.append(i + 2)
        elif value == 'import' and following[0] == 'string':
            refs.append(i + 1)
        else:
            for j in range(i + 1, len(tokens) - 1):
                if tokens[j][1] == ';':
                    break
                if tokens[j] == ('word', 'from') and tokens[j + 1][0] == 'string':
                    refs.append(j + 1)
                    break
    return list(dict.fromkeys(refs))


def relocate_modules(source: str, owner: str, mirror: str, parsed: dict | None = None) -> str:
    """Relocate only actual module literals, keeping comments/other strings untouched."""
    import json
    if parsed is not None and source in parsed:
        raw = source.encode('utf-16-le')
        for item in sorted(parsed[source], key=lambda item:item['start'], reverse=True):
            path = os.path.join(mirror, module_path(owner, item['ref']))
            raw = raw[:item['start'] * 2] + json.dumps(path).replace('%', r'\u0025').encode('utf-16-le') + raw[item['end'] * 2:]
        return raw.decode('utf-16-le')
    positions = {}
    tokens = _tokens(source, positions) if re.search(r'\b(?:import|export)\b', source) else []
    for index in sorted(_module_references(tokens), key=lambda i: positions[i][0], reverse=True):
        start, end = positions[index]
        path = os.path.join(mirror, module_path(owner, tokens[index][1]))
        source = source[:start] + json.dumps(path).replace('%', r'\u0025') + source[end:]
    return source


def module_path(owner: str, ref: str) -> str:
    """Resolve a native module specifier without allowing host files or URL loaders."""
    if (not ref or ref.startswith(('/', '~')) or ':' in ref or '\\' in ref or '\0' in ref
            or re.search(r'%[0-9a-f]{2}', ref, re.I)
            or any(0xd800 <= ord(char) <= 0xdfff for char in ref)):
        raise ValueError('JS imports must use relative paths inside the project')
    target = os.path.normpath(os.path.join(os.path.dirname(owner), ref))
    if target == '..' or target.startswith('../'):
        raise ValueError('a JS module must stay inside the project')
    return target


def module_graph(root: str, entries: list[str], hidden, *, max_files: int = MAX_FILES,
                 max_bytes: int = MAX_BYTES) -> dict[str, bytes]:
    """Snapshot every module once, checking each dependency and bounding total reads."""
    graph, pending, size = {}, list(entries), 0
    while pending:
        rel = pending.pop()
        if rel in graph:
            continue
        if hidden(rel):
            raise ValueError('%s is hidden by the isolation policy' % rel)
        try:
            try:
                data = common.safe_read(root, rel, max_bytes - size + 1, single_link=True)
            except FileNotFoundError:
                # Native module imports accept an omitted .js extension. Never
                # fall back after an unsafe/unreadable existing file or a link.
                if os.path.splitext(rel)[1]:
                    raise
                rel += '.js'
                if hidden(rel):
                    raise ValueError('%s is hidden by the isolation policy' % rel)
                if rel in graph:
                    continue
                data = common.safe_read(root, rel, max_bytes - size + 1, single_link=True)
        except (OSError, ValueError):
            raise ValueError('cannot safely read JS module %s inside the project' % rel) from None
        if len(graph) >= max_files:
            raise ValueError('JS dependency count exceeds the configured limit')
        size += len(data)
        if size > max_bytes:
            raise ValueError('JS dependency size exceeds the configured limit')
        try:
            refs = module_dependencies(data.decode('utf-8-sig'))
        except UnicodeDecodeError:
            raise ValueError('JS modules must contain UTF-8 source') from None
        graph[rel] = data
        for ref in refs:
            pending.append(module_path(rel, ref))
    return graph
