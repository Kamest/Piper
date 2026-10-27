"""HTTP script dependency snapshots: real files, hidden paths, cycles and JS syntax."""
import os
import unittest
import json
import time
import tempfile
from unittest import mock

import _env
import httpdeps


class ModuleDependenciesTest(unittest.TestCase):
    def test_native_response_export_and_next_request_complete_with_selected_java(self):
        if not os.environ.get('PIPER_IJHTTP'):
            self.skipTest('requires the configured native ijhttp')
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        body, hits = b'<feed>\xc4\x8d</feed>\r\n', []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        source = ('GET {{host}}/first\n\n>>! exported.xml\n\n### Next\nGET {{host}}/second\n')
        root = _env.make_project('http-native-export-runtime', {'main.http': source,
            'http-client.env.json': json.dumps({'dev': {'host': 'http://127.0.0.1:%d' % server.server_port}})})
        key = 'team-x/http-native-export-runtime'
        _env.register(key, root)
        manager = _env.manager(self)
        manager.policy['http'] = dict(manager.policy.get('http') or {}, timeout=15)
        result = manager.http_gui_run(key, 'main.http', 'dev', None)
        self.assertTrue(result['ok'], result)
        self.assertEqual(hits, ['/first', '/second'])
        with open(os.path.join(root, 'exported.xml'), 'rb') as output:
            self.assertEqual(output.read(), body)
        with open(os.path.join(root, 'main.http')) as original:
            self.assertEqual(original.read(), source)

    def test_runtime_root_source_map_accounts_for_private_prefix_and_keeps_original_module_names(self):
        root = _env.make_project('http-code-source-map', {})
        source = b'// line one\nthrow Error("line two");\n'
        maps = {}
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as work:
            code = httpdeps.Code('/cli', {}, root, work, {'nested/failing.js':source}, lambda _:False,
                                 lambda *_:None, source_maps=maps)
            try:
                code.add(dict(index=1, phase='pre', site='main.http', origin={}, trigger='script_path',
                              owner='main.http', script_reference='{{name}}'))
                message = dict(kind='script_load', token=next(iter(code.contexts)), reference='nested/failing.js', iteration=None, prefix=10)
                result = code(message, time.monotonic() + 10)
                self.assertEqual(maps[result['path']][11], ('nested/failing.js', 1))
                self.assertEqual(maps[work + '/project/nested/failing.js'][1], ('nested/failing.js', 1))
                with self.assertRaises(ValueError):
                    code(dict(message, prefix=-1), time.monotonic() + 10)
            finally:
                code.close()

    def test_native_discovery_never_accepts_parser_paths_or_skips_hidden_dependencies(self):
        root = _env.make_project('http-native-discovery', {'a.js':'import "./private.js";', 'private.js':'42'})
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as work:
            os.makedirs(os.path.join(work, 'runtime/tmp'))
            discovery = httpdeps.Discovery(root, work, ['a.js'], [], lambda p:p == 'private.js')
            first = discovery(dict(kind='parser_next', index=0), time.monotonic() + 10)
            self.assertEqual(first['name'], 'a.js')
            with open(first['path']) as stream:
                self.assertEqual(stream.read(), 'import "./private.js";')
            with open(os.path.join(work, 'runtime/tmp/parse-0.json'), 'w') as stream:
                json.dump(dict(refs=[dict(ref='./private.js', start=7, end=21)], computed=[]), stream)
            with self.assertRaisesRegex(ValueError, 'hidden'):
                discovery(dict(kind='parser_next', index=1), time.monotonic() + 10)
            self.assertIsNotNone(discovery.failure)
            with self.assertRaisesRegex(ValueError, 'sequence'):
                discovery(dict(kind='parser_next', index=1, path='/anywhere'), time.monotonic() + 10)

    def test_native_parser_discovers_literal_graph_without_running_computed_imports_or_code(self):
        binary = os.environ.get('PIPER_IJHTTP')
        if not binary:
            self.skipTest('requires the configured native ijhttp')
        import httpclient
        sources = {'root.js': 'import "./side.js"; export * from "./other.js"; export * as ns from "./third.js"; '
                             'const x = import("./fourth.js"); import(variable); '
                             'if (true) {} /import(\"ignore.js\")/; const y = {} / 2; '
                             'throw new Error("parse only, never evaluate");',
                   'side.js':'// side', 'other.js':'export const x = 1;', 'third.js':'export const y = 1;',
                   'fourth.js':'export const z = 1;'}
        root = _env.make_project('http-native-parse', sources)
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as work:
            graph, parsed = httpdeps.native_graph(binary, httpclient.java_env(), work, root, ['root.js'], [], lambda _:False)
        self.assertEqual(set(graph), set(sources))
        self.assertEqual([item['ref'] for item in parsed[sources['root.js']]], ['./side.js', './other.js', './third.js', './fourth.js'])
        for item in parsed[sources['root.js']]:
            text = sources['root.js'].encode('utf-16-le')[item['start'] * 2:item['end'] * 2].decode('utf-16-le')
            self.assertEqual(text, json.dumps(item['ref']))

    def test_runtime_code_freezes_literal_dependencies_and_keeps_bytes_after_the_card(self):
        root = _env.make_project('http-runtime-code-unit', {'entry.js':'throw Error("mutable disk");'})
        graph = {'entry.js':b'export {value} from "./dep.js";', 'dep.js':b'export const value=42;'}
        seen = []
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as work:
            code = httpdeps.Code('/cli', {}, root, work, {}, lambda _:False, lambda info, deadline:seen.append(info))
            try:
                code.add(dict(index=0, phase='pre', site='main.http', origin={}, trigger='computed_import'))
                token = next(iter(code.contexts))
                with mock.patch.object(httpdeps, 'native_graph', return_value=(graph, {})):
                    first = code(dict(kind='code_load', token=token, path='entry.js'), time.monotonic() + 20)
                    second = code(dict(kind='code_load', token=token, path='dep.js'), time.monotonic() + 20)
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0]['files'], 2)
                self.assertEqual(seen[0]['deps'][0]['path'], 'dep.js')
                with open(first['path'], 'rb') as stream:
                    self.assertEqual(stream.read(), graph['entry.js'])
                with open(second['path'], 'rb') as stream:
                    self.assertEqual(stream.read(), graph['dep.js'])
                for path in ('../outside.js', '.git/config', 'http-client.private.env.json'):
                    with self.subTest(path=path), self.assertRaises(ValueError):
                        code(dict(kind='code_load', token=token, path=path), time.monotonic() + 20)
            finally:
                code.close()

    def test_each_runtime_graph_keeps_its_own_allowance_while_the_run_cache_remains_bounded(self):
        root = _env.make_project('http-runtime-graph-allowance', {'first.js':'// first', 'second.js':'// second', 'third.js':'// third'})
        seen = []
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as work:
            code = httpdeps.Code('/cli', {}, root, work, {}, lambda _:False, lambda info, _:seen.append(info))
            try:
                code.add(dict(index=0, phase='pre', site='main.http', origin={}, trigger='computed_import'))
                message = dict(kind='code_load', token=next(iter(code.contexts)))
                def graph(binary, env, directory, root, entries, *args, **kwargs):
                    return {entries[0]:b'// approved graph'}, {}
                with mock.patch.object(httpdeps, 'MAX_FILES', 1), mock.patch.object(httpdeps, 'native_graph', side_effect=graph):
                    code(dict(message, path='first.js'), time.monotonic() + 20)
                    code(dict(message, path='second.js'), time.monotonic() + 20)
                    self.assertEqual(len(seen), 2)
                    self.assertEqual(set(code.approved), {'first.js','second.js'})
                    # The cache budget includes empty-file and long-path overhead,
                    # independent of the graph's own file/byte allowance.
                    with mock.patch.object(httpdeps, 'MAX_CODE_CACHE_BYTES', 2048):
                        with self.assertRaisesRegex(ValueError, 'cache exceeds'):
                            code(dict(message, path='third.js'), time.monotonic() + 20)
                    self.assertEqual(len(seen), 2)
                    self.assertNotIn('third.js', code.approved)
            finally:
                code.close()

    def test_import_forms_do_not_follow_comments_strings_or_regular_expressions(self):
        source = '''// import "./comment.js";
const text = "import './string.js'";
const regex = /import \\".\\/regex.js\\"/;
import "./side.js";
import {check as renamed} from './check.js';
export * from "./reexport.js";
const module = await import("./dynamic-literal.js");
'''
        self.assertEqual(httpdeps.module_dependencies(source),
                         ['./side.js', './check.js', './reexport.js', './dynamic-literal.js'])

    def test_relocation_changes_only_module_literals_and_keeps_the_owning_http_directory(self):
        source = '/* import "./false.js" */ const text = "./check.js"; import {x} from "../check.js"; import("./dynamic.js");'
        result = httpdeps.relocate_modules(source, 'api/nested/main.http', '/private/run/project')
        self.assertIn('from "/private/run/project/api/check.js"', result)
        self.assertIn('import("/private/run/project/api/nested/dynamic.js")', result)
        self.assertIn('const text = "./check.js"', result)
        self.assertIn('/* import "./false.js" */', result)
        with self.assertRaisesRegex(ValueError, 'relative'):
            httpdeps.relocate_modules('import "file:///host/private.js"', 'api/main.http', '/private/run')

    def test_nested_graph_and_cycle_are_snapshotted_once(self):
        root = _env.make_project('http-module-graph', {
            'api/handler.js': 'import {check} from "../scripts/check.js"; check();',
            'scripts/check.js': 'export {value} from "./nested/value.js";',
            'scripts/nested/value.js': 'import "../check.js"; export const value = 1;'})
        graph = httpdeps.module_graph(root, ['api/handler.js'], lambda _: False)
        self.assertEqual(set(graph), {'api/handler.js', 'scripts/check.js', 'scripts/nested/value.js'})
        self.assertIn(b'export const value = 1', graph['scripts/nested/value.js'])

    def test_omitted_module_extension_uses_the_native_js_candidate_with_the_same_guards(self):
        root = _env.make_project('http-module-extension', {
            'handler.js': 'import {value} from "./value"; import "./value.js";',
            'value.js': 'export const value = 42;'})
        self.assertEqual(set(httpdeps.module_graph(root, ['handler.js'], lambda _: False, max_files=2)), {'handler.js', 'value.js'})
        with self.assertRaisesRegex(ValueError, 'hidden|cannot safely read'):
            httpdeps.module_graph(root, ['handler.js'], lambda p: p == 'value.js')
        os.symlink('missing', os.path.join(root, 'value'))
        with self.assertRaisesRegex(ValueError, 'cannot safely read'):
            httpdeps.module_graph(root, ['handler.js'], lambda _: False)
        os.unlink(os.path.join(root, 'value'))
        os.link(os.path.join(root, 'value.js'), os.path.join(root, 'value-hardlink.js'))
        with self.assertRaisesRegex(ValueError, 'cannot safely read'):
            httpdeps.module_graph(root, ['handler.js'], lambda _: False)

    def test_every_dependency_respects_hidden_and_project_boundaries(self):
        root = _env.make_project('http-module-hidden', {
            'api/handler.js': 'import "../private/secret.js";', 'private/secret.js': 'synthetic-secret'})
        with self.assertRaisesRegex(ValueError, 'hidden'):
            httpdeps.module_graph(root, ['api/handler.js'], lambda p: p.startswith('private/'))
        for ref in ('../../outside.js', '/outside.js', 'file:///outside.js', 'https://outside.example/code.js'):
            with open(os.path.join(root, 'api/handler.js'), 'w') as output:
                output.write('import ' + __import__('json').dumps(ref) + ';')
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, 'project|relative'):
                httpdeps.module_graph(root, ['api/handler.js'], lambda _: False)

    def test_symlinked_module_cannot_read_a_file_outside_the_project(self):
        root = _env.make_project('http-module-links', {'handler.js': 'import "./link.js";'})
        target = os.path.join(_env.ROOT, 'private-module.js')
        with open(target, 'w') as output:
            output.write('synthetic-private-value')
        os.symlink(target, os.path.join(root, 'link.js'))
        with self.assertRaises(ValueError):
            httpdeps.module_graph(root, ['handler.js'], lambda _: False)

    def test_computed_imports_are_reported_before_execution_and_graph_bounds_are_explicit(self):
        for source in ('import(variable)', 'import(`./${variable}.js`)',
                       'const text = `${await import(variable)}`;'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'literal'):
                httpdeps.module_dependencies(source)
        root = _env.make_project('http-module-bounds', {'handler.js': 'import "./helper.js";', 'helper.js': '// code'})
        with self.assertRaisesRegex(ValueError, 'count'):
            httpdeps.module_graph(root, ['handler.js'], lambda _: False, max_files=1)
        with self.assertRaisesRegex(ValueError, 'size'):
            httpdeps.module_graph(root, ['handler.js'], lambda _: False, max_bytes=4)

    def test_property_methods_are_not_module_loaders_and_escaped_specifiers_are_resolved(self):
        self.assertEqual(httpdeps.module_dependencies('obj.import(variable); obj.export("text");'), [])
        self.assertEqual(httpdeps.module_dependencies(r'import "./\u0068elper.js";'), ['./helper.js'])
        self.assertEqual(httpdeps.module_dependencies('const text = `${await import("./helper.js")}`;'), ['./helper.js'])
        for ref in ('./%2e%2e/secret.js', './bad\0.js', './bad\ud800.js'):
            with self.subTest(ref=repr(ref)), self.assertRaises(ValueError):
                httpdeps.module_path('api/handler.js', ref)

    def test_scripts_without_module_keywords_keep_native_javascript_grammar(self):
        for source in ('const x = /[[]/;', 'const x = /pattern/v;', 'const x = {} / 2;',
                       'const x = () => /[[]/;'):
            with self.subTest(source=source):
                self.assertEqual(httpdeps.module_dependencies(source), [])

    def test_missing_or_unreadable_module_and_symlinked_parent_are_rejected(self):
        root = _env.make_project('http-module-types', {'handler.js': 'import "./missing.js";'})
        with self.assertRaisesRegex(ValueError, 'cannot safely read'):
            httpdeps.module_graph(root, ['handler.js'], lambda _: False)
        os.mkdir(os.path.join(root, 'nested'))
        os.symlink('nested', os.path.join(root, 'alias'))
        with self.assertRaisesRegex(ValueError, 'cannot safely read'):
            httpdeps.module_graph(root, ['alias/missing.js'], lambda _: False)

    def test_division_after_strings_and_keyword_properties_cannot_hide_dynamic_imports(self):
        for source in ('"return" / import(path);', 'object.return / import(path);',
                       'object.case / import(path);', 'object.#yield / import(path);'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'literal'):
                httpdeps.module_dependencies(source)
        self.assertEqual(httpdeps.module_dependencies('"=" / import("./dependency.js");'), ['./dependency.js'])

    def test_regex_after_expression_operators_and_control_flow_cannot_hide_imports(self):
        for prefix in ('const f = () =>', 'if (true)', 'while (false)', 'for (;false;)', 'for await (const x of xs)',
                       '0 +', '0 ||', 'void', 'break\n', 'continue\n', 'debugger\n', 'if (true) {}'):
            # Both regex literals are valid JS. Treating their quote as a string would
            # swallow the real import between them and bypass the dependency preflight.
            source = prefix + " /'/; import(path); /'/;"
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, 'literal|ambiguous'):
                httpdeps.module_dependencies(source)
        for source in ('value++ / import(path);', 'value-- / import(path);', 'object.throw / import(path);'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'literal'):
                httpdeps.module_dependencies(source)
        for newline in ('\r', '\n', '\u2028', '\u2029'):
            with self.subTest(newline=repr(newline)), self.assertRaisesRegex(ValueError, 'literal'):
                httpdeps.module_dependencies('// comment' + newline + 'import(path);')
        for source in ('export const x = /[[]/;', 'export const x = /pattern/v;', 'of / import(path);'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'ambiguous'):
                httpdeps.module_dependencies(source)



if __name__ == '__main__':
    unittest.main()
