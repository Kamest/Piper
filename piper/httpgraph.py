"""Freeze native HTTP run/import dependencies before approval, without running scripts."""
from __future__ import annotations

import os
import re
import hashlib
import secrets

import common

MAX_FLAT = 16 << 20


class Graph:
    def __init__(self, root, rel, text, index, values, hidden, parser, substitute):
        self.root, self.rel = os.path.realpath(root), rel
        self.parser, self.substitute, self.hidden = parser, substitute, hidden
        self.docs, self.selected, self.leaves, self.runs = {}, {}, [], {}
        self.nodes = []
        self.native_nonce = secrets.token_hex(16)
        self.size = 0
        self.no_log = False
        self._add(rel, text)
        self._visit(rel, index, {}, values, [])

    def path(self, owner, ref):
        if ref.startswith(('~', '/')) or '\\' in ref or any(ord(c) < 32 for c in ref) or '{{' in ref:
            raise ValueError('HTTP import must use a fixed relative path inside the project')
        path = os.path.normpath(os.path.join(os.path.dirname(owner), ref)).replace(os.sep, '/')
        if path == '..' or path.startswith('../') or not path.lower().endswith(('.http', '.rest')):
            raise ValueError('HTTP import must stay inside the project and name an HTTP file')
        if self.hidden(path):
            raise ValueError('%s is hidden by the isolation policy' % path)
        return path

    def _add(self, rel, text=None):
        if rel in self.docs:
            return
        if len(self.docs) >= 128:
            raise ValueError('HTTP dependency count exceeds 128 files')
        if text is None:
            try:
                raw = common.safe_read(self.root, rel, (1 << 20) + 1)
                text = raw.decode('utf-8-sig')
            except (OSError, ValueError):
                raise ValueError('cannot safely read HTTP import %s' % rel) from None
        if len(text.encode()) > 1 << 20:
            raise ValueError('HTTP import exceeds 1 MiB')
        self.size += len(text.encode())
        if self.size > 16 << 20:
            raise ValueError('HTTP dependency size exceeds 16 MiB')
        text = re.sub(r'\r(?!\n)', '\n', text)
        parsed = self.parser(text)
        self.docs[rel] = text, parsed
        for declaration in parsed['imports']:
            self._add(self.path(rel, declaration['path']))

    @staticmethod
    def invocation(url):
        match = re.fullmatch(r'(.+?)(?:\s+\((.*)\))?', url.strip())
        if not match:
            raise ValueError('invalid HTTP run declaration')
        target, raw = match.groups()
        overrides = {}
        if raw is not None:
            for item in re.split(r',\s*(?=@)', raw):
                assignment = re.fullmatch(r'\s*@([\w.-]+)\s*=\s*(.*?)\s*', item)
                if not assignment or not assignment[1].isascii() or assignment[1] in overrides:
                    raise ValueError('invalid or duplicate HTTP run variable override')
                overrides[assignment[1]] = assignment[2]
        return target, overrides

    def _named(self, rel, name, seen=None):
        seen = set() if seen is None else seen
        if rel in seen:
            return []
        seen.add(rel)
        _, parsed = self.docs[rel]
        found = [(rel, r['index']) for r in parsed['requests'] if r.get('id') == name]
        for declaration in parsed['imports']:
            found += self._named(self.path(rel, declaration['path']), name, seen)
        return found

    def _visit(self, rel, index, overrides, environment, active, root_index=None):
        text, parsed = self.docs[rel]
        if len(self.nodes) >= 2048:
            raise ValueError('HTTP run graph exceeds 2048 invocations')
        node = dict(rel=rel, entries=[], number=len(self.nodes))
        self.nodes.append(node)
        values, file_values = dict(environment), {}
        for var in parsed['vars']:
            file_values[var['name']] = self.substitute(overrides.get(var['name'], var['value']),
                                                     dict(values, **file_values))
        file_values.update(overrides)
        values.update(file_values)
        selected = parsed['requests'] if index is None else [parsed['requests'][index]]
        for request in selected:
            entry_index = request['index'] if root_index is None else root_index
            identity = rel, request['index']
            if identity in active:
                raise ValueError('cyclic HTTP run dependency at %s' % rel)
            self.selected.setdefault(rel, set()).add(request['index'])
            self.no_log |= request['no_log']
            if request['method'].lower() == 'run':
                target, params = self.invocation(request['url'])
                params = {key: self.substitute(value, values) for key, value in params.items()}
                if target.startswith('#'):
                    matches = self._named(rel, target[1:])
                    if len(matches) != 1:
                        raise ValueError('HTTP named run needs one unambiguous request: %s' % target)
                    child, child_index = matches[0]
                else:
                    child, child_index = self.path(rel, target), None
                    self._add(child)
                self.runs[identity] = child, child_index
                child_node = self._visit(child, child_index, params, environment, active + [identity], entry_index)
                node['entries'].append(dict(request=request, child=child_node))
            else:
                if len(self.leaves) >= 2048:
                    raise ValueError('HTTP run graph exceeds 2048 requests')
                self.leaves.append(dict(identity=identity, request=request, owner=rel, values=values, root_index=entry_index,
                                        file_values=file_values))
                node['entries'].append(dict(request=request, leaf=len(self.leaves) - 1))
        return node

    def flatten(self):
        """Private preflight view. Each block retains its original file-variable definitions."""
        lines, sources = [], []
        flat_size = 0
        scripts_before = False
        for leaf in self.leaves:
            owner = leaf['owner']
            text, parsed = self.docs[owner]
            original, request = text.split('\n'), leaf['request']
            start = len(lines)
            lines.append('### ' + request['name'])
            sources.append((owner, request['start']))
            for var in parsed['vars']:
                lines.append(original[var['line']])
                sources.append((owner, var['line']))
            scripts_before |= bool(request['pre'] or len(request['scripts']) > int(bool(request['handler_file'])))
            refs = {}
            for ref in request['files']:
                if scripts_before and re.search(r'\{\{\s*(?!\$)[^{}]+\}\}', ref):
                    continue  # resolved once by the appropriate runtime data/code broker
                path = self.substitute(ref, dict(leaf['values'], **{'$projectRoot':self.root}))
                if '{{' in path:
                    continue
                if path.startswith('~'):
                    raise ValueError('HTTP dependency path must be known before the run')
                path = os.path.normpath(path if os.path.isabs(path) else os.path.join(self.root, os.path.dirname(owner), path))
                canonical = os.path.relpath(path, self.root)
                if canonical == '..' or canonical.startswith('../') or self.hidden(canonical):
                    raise ValueError('HTTP dependency is hidden or outside the project')
                refs[ref] = path
            common_lines = {i for block in parsed['common'] for i in range(block['start'], block['end'] + 1)}
            for i in range(request['start'], request['end']):
                if i in common_lines:
                    continue
                line = original[i]
                if i == request['start'] and line.startswith('###') or any(v['line'] == i for v in parsed['vars']):
                    continue
                for ref, path in refs.items():
                    if re.fullmatch(r'\s*[<>]@?\s+' + re.escape(ref) + r'\s*', line):
                        line = line[:line.index(ref)] + path
                        break
                lines.append(line)
                sources.append((owner, i))
            lines.append('')
            sources.append((owner, request['end'] - 1))
            leaf['flat_start'] = start
            flat_size += sum(len(line.encode('utf-8')) + 1 for line in lines[start:])
            if flat_size > MAX_FLAT:
                raise ValueError('expanded HTTP run graph exceeds 16 MiB')
            scripts_before |= bool(request['handler'] or request['handler_file'])
        flat = '\n'.join(lines)
        parsed = self.parser(flat)
        if len(parsed['requests']) != len(self.leaves):
            raise ValueError('HTTP dependency structure cannot be represented safely')
        for request, leaf in zip(parsed['requests'], self.leaves):
            request['owner'] = leaf['owner']
            request['url'] = self.substitute(request['url'], leaf['values'])
            request['file_values'] = leaf['file_values']
            request['root_index'] = leaf['root_index']
            request['source_line'] = leaf['request']['line'] + 1
        self.sources = sources
        return flat, parsed

    def stamps(self):
        """One immutable source digest per root selection, including imported definitions."""
        def imports(rel, found):
            if rel in found:
                return
            found.add(rel)
            for declaration in self.docs[rel][1]['imports']:
                imports(self.path(rel, declaration['path']), found)
        def descendants(node, found):
            imports(node['rel'], found)
            for entry in node['entries']:
                if 'child' in entry:
                    descendants(entry['child'], found)
        result = {}
        for entry in self.nodes[0]['entries']:
            found = set()
            imports(self.rel, found)
            if 'child' in entry:
                descendants(entry['child'], found)
            digest = hashlib.sha256()
            for rel in sorted(found):
                digest.update(rel.encode() + b'\0' + self.docs[rel][0].encode() + b'\0')
            result[str(entry['request']['index'])] = digest.hexdigest()
        return result

    def write(self, work, rows, transform=lambda value: value):
        """Retain native file scopes/run overrides; only selected code exists in the mirror."""
        parsed = self.parser('\n'.join(text for text, _ in rows))
        replacements = {}
        for number, request in enumerate(parsed['requests']):
            replacements[number] = rows[request['start']:request['end']]
        def native_path(node):
            rel = node['rel']
            if node['number']:
                rel = os.path.join(os.path.dirname(rel), '.piper-run-%s-%d.http' % (self.native_nonce, node['number']))
            return os.path.join(work, 'project', rel)
        previous_paths = set(getattr(self, 'native_maps', {}))
        self.native_maps = {}
        for node in self.nodes:
            rel = node['rel']
            text, doc = self.docs[rel]
            original = text.split('\n')
            # Each invocation gets its own private file, preserving native file scopes
            # while instrumentation and exports retain an unambiguous response identity.
            result = [(original[v['line']], (rel, v['line'])) for v in doc['vars']]
            for entry in node['entries']:
                request = entry['request']
                if 'leaf' in entry:
                    variable_lines = {v['line'] for v in doc['vars']}
                    for line, origin in replacements[entry['leaf']]:
                        source = (origin if isinstance(origin, tuple) else
                                  self.sources[origin] if origin is not None and 0 <= origin < len(self.sources) else None)
                        if source and source[0] == rel and source[1] in variable_lines and line == original[source[1]]:
                            continue  # declarations already precede the native file's first request
                        result.append((line, source))
                else:
                    result.append(('### ' + request['name'], (rel, request['start'])))
                    target, _ = self.invocation(request['url'])
                    declaration = original[request['line']]
                    result.append((declaration.replace(target, native_path(entry['child']), 1), (rel, request['line'])))
            path = native_path(node)
            self.native_maps[path] = rel, [origin for _, origin in result]
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            # Never overwrite an input snapshot on an unexpected name collision.
            mode = 'w' if not node['number'] or path in previous_paths else 'x'
            with open(path, mode, encoding='utf-8') as output:
                output.write(transform('\n'.join(line for line, _ in result) + '\n'))

    def relink(self, message):
        """Relink private invocation files and injected line offsets to authored sources."""
        for path, (owner, origins) in sorted(self.native_maps.items(), key=lambda item: -len(item[0])):
            def replace(match):
                index = int(match[1]) - 1
                while 0 <= index < len(origins) and origins[index] is None:
                    index -= 1
                source = origins[index] if 0 <= index < len(origins) else None
                return '%s:%d' % (source[0], source[1] + 1) if source else owner
            message = re.sub(re.escape(path) + r':(\d+)', replace, message).replace(path, owner)
        return message
