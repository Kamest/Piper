"""Recurring project inventory must share scans across roots and HTTP threads."""
import concurrent.futures
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env  # noqa: F401
import common
import extensions
import piperd
import projects
from test_piperd import FakeManager, get


class DiscoveryPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.roots = [os.path.join(self.tmp.name, name) for name in ('alpha', 'beta', 'gamma')]
        for root in self.roots:
            os.makedirs(os.path.join(root, 'team', 'shop', '.git'))
        self.previous = common.read_json(common.PROJECTS_FILE, {})
        self.addCleanup(common.write_json, common.PROJECTS_FILE, self.previous)
        common.write_json(common.PROJECTS_FILE, {'globs': self.roots[:2]})
        projects._DISCOVERED['at'] = 0
        self.addCleanup(projects._DISCOVERED.__setitem__, 'at', 0)

    def test_alternating_roots_reuse_their_own_scan(self):
        with mock.patch.object(projects.os, 'scandir', wraps=os.scandir) as scan:
            expected = projects.load(self.roots[2])
            cold = scan.call_count
            for _ in range(10):
                self.assertEqual(projects.load(self.roots[2]), expected)
            self.assertEqual(scan.call_count, cold, 'warm state polls must not rewalk alternating roots')

    def test_overlapping_inventory_sources_resolve_each_path_once_per_load(self):
        root = self.roots[0]
        target = os.path.join(root, 'team', 'shop')
        common.write_json(common.PROJECTS_FILE, {'projects': {'chosen': {'path': target}},
                                                'globs': [root + '/*', root + '/*']})
        expected = projects.load(root)
        with mock.patch.object(os.path, 'realpath', wraps=os.path.realpath) as resolve:
            self.assertEqual(projects.load(root), expected)
        self.assertEqual(list(expected), ['chosen'])
        self.assertEqual(sum(c.args == (target,) for c in resolve.call_args_list), 1,
                         'configured projects, overlapping globs and discovery must share this load snapshot')

    def test_inventory_path_snapshot_does_not_hide_retargeted_links_or_configuration_edits(self):
        link = os.path.join(self.tmp.name, 'linked')
        first = os.path.join(self.roots[0], 'team', 'shop')
        second = os.path.join(self.roots[1], 'team', 'shop')
        os.symlink(first, link)
        common.write_json(common.PROJECTS_FILE, {'projects': {'chosen': {'path': link, 'title': 'First'}}})
        self.assertEqual(projects.load('')['chosen']['path'], first)
        os.unlink(link)
        os.symlink(second, link)
        common.write_json(common.PROJECTS_FILE, {'projects': {'chosen': {'path': link, 'title': 'Second'}}})
        p = projects.load('')['chosen']
        self.assertEqual((p['path'], p['title']), (second, 'Second'))

    def test_twenty_simultaneous_polls_share_cold_scans(self):
        barrier = threading.Barrier(20)
        scan = os.scandir
        counts = []
        def slow_scan(path):
            counts.append(path)
            time.sleep(.003)
            return scan(path)
        def poll(_):
            barrier.wait(timeout=5)
            return projects.load(self.roots[2])
        with mock.patch.object(projects.os, 'scandir', side_effect=slow_scan):
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                rows = list(pool.map(poll, range(20)))
        self.assertTrue(all(row == rows[0] for row in rows))
        self.assertEqual(len(counts), 6, 'each root and team is scanned once across all requests')

    def test_depth_skip_and_expiry_are_independent(self):
        root = self.roots[0]
        self.assertEqual(projects.discover(root, max_depth=1), [])
        self.assertEqual(len(projects.discover(root, max_depth=3)), 1)
        with mock.patch.object(extensions, 'items', return_value=['team']):
            self.assertEqual(projects.discover(root), [])
        self.assertEqual(len(projects.discover(root)), 1)
        os.makedirs(os.path.join(root, 'new', '.git'))
        self.assertEqual(len(projects.discover(root)), 1)
        self.assertEqual(len(projects.discover(root, ttl=0)), 2)

    def test_cache_is_bounded_and_expiry_does_not_slide_with_hits(self):
        root = self.roots[0]
        with mock.patch.object(projects.time, 'monotonic', return_value=100):
            projects.discover(root)
        os.makedirs(os.path.join(root, 'new', '.git'))
        with mock.patch.object(projects.time, 'monotonic', return_value=159):
            self.assertEqual(len(projects.discover(root)), 1)
        with mock.patch.object(projects.time, 'monotonic', return_value=161):
            self.assertEqual(len(projects.discover(root)), 2)
        for i in range(300):
            projects.discover(os.path.join(self.tmp.name, 'missing-%s' % i))
        self.assertLessEqual(len(projects._DISCOVERY_CACHE), 256)

    def test_find_miss_refreshes_new_checkout_and_skipped_is_root_specific(self):
        root = self.roots[2]
        projects.load(root)
        os.makedirs(os.path.join(root, 'new', '.git'))
        self.assertIsNotNone(projects.find('new', root))
        os.makedirs(os.path.join(root, 'bad name', '.git'))
        projects._DISCOVERED['at'] = 0
        projects.discover(root)
        projects.discover(self.roots[0])
        self.assertIn('bad name', projects.skipped(root))

    def test_state_uses_one_project_inventory_for_defaults_and_list(self):
        token = common.own_token('gui')
        path = os.path.join(self.roots[2], 'team', 'shop')
        common.write_json(common.PROJECTS_FILE, {'projects': {'shop': {'path': path}},
            'session_defaults': {'shop': {'path': path, 'agent': 'codex', 'opts': ['idea']}}})
        with mock.patch.object(piperd, 'MANAGER', FakeManager()), \
                mock.patch.object(projects, 'load', wraps=projects.load) as load:
            code, _, raw = get('/api/state', token=token)
        self.assertEqual(code, 200)
        self.assertIn('projects', json.loads(raw))
        self.assertEqual(json.loads(raw)['project_session_defaults']['shop'], {'agent': 'codex', 'opts': ['idea']})
        self.assertEqual(load.call_count, 1)


if __name__ == '__main__':
    unittest.main()
