"""Native run/import snapshots keep file scopes and freeze every checked dependency."""
import os
import unittest
from unittest import mock

import _env
import httpclient as hc
from httpgraph import Graph


class GraphTest(unittest.TestCase):
    def build(self, files, *, index=None, hidden=lambda _: False, rel='api/main.http'):
        root = _env.make_project('http-graph-' + __import__('secrets').token_hex(4), files)
        graph = Graph(root, rel, files[rel], index,
                      {'host':'https://approved.example'}, hidden, hc.parse, hc.substitute)
        return root, graph

    def test_imports_are_not_requests_and_consecutive_runs_are_discovered(self):
        parsed = hc.parse('import ./defs.http\nrun #one (@id=x)\nrun ./other.http\n')
        self.assertEqual([r['method'] for r in parsed['requests']], ['run', 'run'])
        self.assertEqual(parsed['imports'], [{'path':'./defs.http', 'line':0}])
        parsed = hc.parse('POST https://example.test\n\nrun ./literal-body.http\n')
        self.assertEqual(len(parsed['requests']), 1)
        self.assertIn('run ./literal', parsed['requests'][0]['body'])

    def test_nested_graph_named_request_override_and_source_relative_refs(self):
        root, graph = self.build({
            'api/main.http': 'import ./defs.http\n### Selected\nrun #named (@id=override)\n\n### Nested\nrun ./sub/flow.http\n',
            'api/defs.http': '@id=default\n### Named\n# @name named\nGET {{host}}/{{id}}\n\n>>! named.xml\n',
            'api/sub/flow.http': 'run ./body.http\n',
            'api/sub/body.http': 'POST {{host}}/upload\n\n< ./payload.json\n',
            'api/sub/payload.json':'{"safe":true}'})
        flat, parsed = graph.flatten()
        self.assertEqual([r['url'] for r in parsed['requests']],
                         ['https://approved.example/override', 'https://approved.example/upload'])
        self.assertEqual(parsed['requests'][1]['files'], [root + '/api/sub/payload.json'])
        work = os.path.join(_env.ROOT, 'http-graph-native-copy')
        os.makedirs(work)
        graph.write(work, [(line, i) for i, line in enumerate(flat.split('\n'))])
        snapshot = common_read(work, 'project/api/main.http')
        self.assertIn('run ' + work + '/project/api/.piper-run-' + graph.native_nonce + '-1.http (@id=override)', snapshot)
        self.assertNotIn('import ', snapshot)
        self.assertIn('run ' + work + '/project/api/sub/.piper-run-' + graph.native_nonce + '-2.http', snapshot)
        self.assertEqual(common_read(root, 'api/main.http'), graph.docs['api/main.http'][0])
        private = next(path for path, (owner, _) in graph.native_maps.items() if owner == 'api/defs.http')
        origins = graph.native_maps[private][1]
        original_line = graph.docs['api/defs.http'][1]['requests'][0]['line']
        private_line = origins.index(('api/defs.http', original_line)) + 1
        self.assertEqual(graph.relink(private + ':' + str(private_line) + ': error'),
                         'api/defs.http:' + str(original_line + 1) + ': error')

    def test_cycles_missing_hidden_outside_symlinks_and_ambiguous_names_fail_before_run(self):
        cases = [
            {'api/main.http':'run ../outside/no.http\n'},
            {'api/main.http':'run ./missing.http\n'},
            {'api/main.http':'run ./cycle.http\n', 'api/cycle.http':'run ./main.http\n'},
            {'api/main.http':'import ./a.http\nimport ./b.http\nrun #name\n',
             'api/a.http':'# @name name\nGET {{host}}/a', 'api/b.http':'# @name name\nGET {{host}}/b'}]
        for files in cases:
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.build(files)
        with self.assertRaisesRegex(ValueError, 'hidden'):
            self.build({'api/main.http':'run ./secret.http\n','api/secret.http':'GET {{host}}/secret'},
                       hidden=lambda path:path.endswith('secret.http'))

    def test_injected_common_declarations_keep_their_own_source_through_native_graph(self):
        _, graph = self.build({'api/main.http':'run ./child.http\n',
                               'api/child.http':'={%\nthrow Error("common");\n%}\nGET {{host}}/child\n\n> {% client.log("handler"); %}\n'})
        flat, parsed = graph.flatten()
        rows = hc.common_rows([(line, i) for i, line in enumerate(flat.split('\n'))], parsed['requests'],
                              [graph.docs['api/child.http'][1]['common']], {}, '/private/work', ['api/child.http'])
        work = os.path.join(_env.ROOT, 'http-graph-common-source')
        graph.write(work, rows)
        private = next(path for path, (owner, _) in graph.native_maps.items() if owner == 'api/child.http')
        line = common_read(work, os.path.relpath(private, work)).split('\n').index('throw Error("common");') + 1
        self.assertEqual(graph.relink(private + ':' + str(line)), 'api/child.http:2')

    def test_runtime_script_paths_retain_their_original_owner_in_imported_flows(self):
        _, graph = self.build({'api/main.http':'run ./nested/child.http\n',
            'api/nested/child.http':'< {% client.global.set("handler", "post.js"); %}\nGET {{host}}/child\n\n> {{handler}}\n'})
        flat, parsed = graph.flatten()
        self.assertIn('> {{handler}}', flat)
        self.assertEqual(parsed['requests'][0]['owner'], 'api/nested/child.http')
        self.assertEqual(parsed['requests'][0]['handler_file'], '{{handler}}')

    def test_single_run_only_snapshots_its_selected_branch_and_no_log_propagates(self):
        _, graph = self.build({'api/main.http':'### One\n# @no-log\nrun ./a.http\n\n### Other\nrun ./missing.http\n',
                               'api/a.http':'GET {{host}}/a\n'}, index=0)
        self.assertEqual(len(graph.leaves), 1)
        self.assertTrue(graph.no_log)
        self.assertNotIn('api/missing.http', graph.docs)

    def test_local_named_and_repeated_import_invocations_have_separate_native_snapshots(self):
        _, graph = self.build({'api/main.http':'@id=local\n# @name one\nGET {{host}}/{{id}}\n\n### Again\n'
                               'run #one (@id=second)\nrun #one (@id=third)\n'})
        flat, parsed = graph.flatten()
        self.assertEqual([r['url'] for r in parsed['requests']], [
            'https://approved.example/local', 'https://approved.example/second', 'https://approved.example/third'])
        self.assertEqual([r['root_index'] for r in parsed['requests']], [0, 1, 2])
        work = os.path.join(_env.ROOT, 'http-graph-repeated')
        graph.write(work, [(line, i) for i, line in enumerate(flat.split('\n'))])
        self.assertIn('(@id=second)', common_read(work, 'project/api/main.http'))
        self.assertIn('GET {{host}}/{{id}}', common_read(work, 'project/api/.piper-run-' + graph.native_nonce + '-1.http'))
        self.assertIn('GET {{host}}/{{id}}', common_read(work, 'project/api/.piper-run-' + graph.native_nonce + '-2.http'))

    def test_repeated_sources_cannot_bypass_the_expanded_graph_memory_bound(self):
        _, graph = self.build({'api/main.http':'run ./body.http\nrun ./body.http\n',
                               'api/body.http':'POST {{host}}/body\n\n' + 'x' * 100 + '\n'})
        with mock.patch('httpgraph.MAX_FLAT', 200), self.assertRaisesRegex(ValueError, 'expanded HTTP run graph'):
            graph.flatten()

    def test_private_invocation_names_never_overwrite_a_same_named_root_or_input_snapshot(self):
        rel = 'api/.piper-run-1.http'
        _, graph = self.build({rel:'run ./child.http\n', 'api/child.http':'GET {{host}}/child\n'}, rel=rel)
        flat, _ = graph.flatten()
        work = os.path.join(_env.ROOT, 'http-graph-name-collision')
        rows = [(line, i) for i, line in enumerate(flat.split('\n'))]
        graph.write(work, rows)
        self.assertIn('run ', common_read(work, 'project/' + rel))
        self.assertEqual(len(graph.native_maps), 2)
        graph.write(work, rows)  # the host's tunnel rewrite may update its own prior copies
        child = next(path for path, (owner, _) in graph.native_maps.items() if owner=='api/child.http')
        fresh = Graph(graph.root, rel, graph.docs[rel][0], None, {'host':'https://approved.example'},
                      lambda _:False, hc.parse, hc.substitute)
        fresh.native_nonce = graph.native_nonce
        fresh.flatten()
        with self.assertRaises(FileExistsError):
            fresh.write(work, rows)
        self.assertIn('GET {{host}}/child', common_read(work, os.path.relpath(child,work)))


def common_read(root, rel):
    import common
    return common.safe_read(root, rel, 1 << 20).decode()
