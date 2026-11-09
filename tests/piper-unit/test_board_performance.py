"""Board refreshes share a Codex file index instead of searching the archive per session."""
import json
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import _env
import common
import digest
import gitops
import sessions


class BoardPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(mock.patch.object(common, 'REAL_HOME', self.tmp.name))
        self.manager = _env.manager(self)
        self.cfg = Path(self.tmp.name, '.codex')
        for day in range(1, 29):
            (self.cfg / 'sessions/2026/10' / ('%02d' % day)).mkdir(parents=True)
        self.rows = []
        for i in range(20):
            native = str(uuid.uuid4())
            s = sessions.Session(kind='advisor', agent='codex', claude_mode='none', host_codex=True,
                                 claude_session=native, state='exited')
            self.manager.sessions[s.id] = s
            self.rows.append(s)
            if i < 12:
                self.write(s, 'sessions/2026/10/01', 'Answer %s' % i)

    def write(self, s, folder, text):
        p = self.cfg / folder / ('rollout-demo-' + s.claude_session + '.jsonl')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                      'content': [{'type': 'output_text', 'text': text}]}}) + '\n')
        return p

    def test_two_refreshes_walk_each_shared_archive_once_including_missing_sessions(self):
        with mock.patch.object(common, 'safe_listdir', wraps=common.safe_listdir) as scan:
            first = self.manager._board()
            cold = scan.call_count
            second = self.manager._board()
            warm = scan.call_count - cold
        self.assertEqual({r['id']: r['last'] for r in first}, {r['id']: r['last'] for r in second})
        self.assertEqual(len(first), 20)
        self.assertTrue(any(r['last'] == 'Answer 0' for r in first))
        self.assertLessEqual(cold, 33, 'one root index, not one walk per session and parser')
        self.assertLessEqual(warm, 33, 'missing histories must share the same request-local index too')

    def test_next_refresh_sees_new_files_archival_edits_and_refuses_symlinks(self):
        self.manager._board()
        s = self.rows[0]
        p = self.cfg / 'sessions/2026/10/01' / ('rollout-demo-' + s.claude_session + '.jsonl')
        archive = self.cfg / 'archived_sessions' / p.name
        archive.parent.mkdir()
        p.rename(archive)
        self.write(self.rows[12], 'sessions/2026/10/02', 'New history')
        rows = {r['id']: r for r in self.manager._board()}
        self.assertEqual(rows[s.id]['last'], 'Answer 0')
        self.assertEqual(rows[self.rows[12].id]['last'], 'New history')
        self.write(s, 'archived_sessions', 'Changed history')
        rows = {r['id']: r for r in self.manager._board()}
        self.assertEqual(rows[s.id]['last'], 'Changed history')
        archive.unlink()
        target = Path(self.tmp.name, 'outside.jsonl')
        target.write_text('{"type":"response_item","payload":{"type":"message","role":"assistant",'
                          '"content":[{"type":"output_text","text":"outside"}]}}\n')
        archive.symlink_to(target)
        rows = {r['id']: r for r in self.manager._board()}
        self.assertEqual(rows[s.id]['last'], '')

    def test_index_lookup_preserves_direct_lookup_priority_and_no_symlink_fallback(self):
        s = self.rows[0]
        archive = self.write(s, 'archived_sessions', 'Archived')
        self.assertEqual(digest.history_file_map(str(self.cfg), 'codex')[s.claude_session],
                         digest.session_file(str(self.cfg), s.claude_session, 'codex'))
        archive.unlink()
        archive.symlink_to(self.cfg / 'sessions/2026/10/01' / archive.name)
        self.assertEqual(digest.history_file_map(str(self.cfg), 'codex')[s.claude_session],
                         digest.session_file(str(self.cfg), s.claude_session, 'codex'))

    def test_growing_rollout_reads_recent_records_and_retains_the_last_400_message_window(self):
        s = self.rows[0]
        path = self.cfg / 'sessions/2026/10/01' / ('rollout-demo-' + s.claude_session + '.jsonl')
        message = lambda role, text: {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                                    'content': [{'type': 'output_text', 'text': text}]}}
        noise = json.dumps({'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x' * 8192}}) + '\n'
        path.write_text(noise * 512 + json.dumps(message('assistant', 'Previous answer')) + '\n')
        self.manager._board()
        with path.open('a') as f:
            f.write(json.dumps(message('user', '[chat m12] New request')) + '\n')
        byte_count = 0
        original = common.safe_read
        def read(root, rel, *args, **kwargs):
            nonlocal byte_count
            data = original(root, rel, *args, **kwargs)
            if root == str(self.cfg) and rel == str(path.relative_to(self.cfg)):
                byte_count += len(data)
            return data
        with mock.patch.object(common, 'safe_read', side_effect=read):
            rows = {r['id']: r for r in self.manager._board()}
        self.assertEqual(rows[s.id]['last'], 'Previous answer')
        self.assertEqual(rows[s.id]['asked'], 'New request')
        self.assertLess(byte_count, 16384, 'a short recent exchange must not reparse megabytes of old tool results')
        with path.open('a') as f:
            f.write((json.dumps(message('user', 'Request')) + '\n') * 400)
        rows = {r['id']: r for r in self.manager._board()}
        self.assertEqual(rows[s.id]['last'], '', 'the old answer fell outside the same 400-message window')

    def test_incremental_preview_waits_for_complete_lines_and_resets_on_replacement_or_rewrite(self):
        s = self.rows[0]
        path = self.cfg / 'sessions/2026/10/01' / ('rollout-demo-' + s.claude_session + '.jsonl')
        self.manager._board()
        raw = json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                          'content': [{'type': 'output_text', 'text': 'Appended answer'}]}})
        with path.open('a') as f:
            f.write(raw[:60])
        self.assertEqual({r['id']: r for r in self.manager._board()}[s.id]['last'], 'Answer 0')
        with path.open('a') as f:
            f.write(raw[60:] + '\n')
        self.assertEqual({r['id']: r for r in self.manager._board()}[s.id]['last'], 'Appended answer')
        self.write(s, 'sessions/2026/10/01', 'Rewritten answer')
        self.assertEqual({r['id']: r for r in self.manager._board()}[s.id]['last'], 'Rewritten answer')
        old = path.with_suffix('.old')
        path.rename(old)
        self.write(s, 'sessions/2026/10/01', 'Replacement answer')
        self.assertEqual({r['id']: r for r in self.manager._board()}[s.id]['last'], 'Replacement answer')


class BoardGitPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = _env.manager(self)
        self.repos = []
        self.rows = []
        for index in range(2):
            root = Path(self.tmp.name, 'repo%d' % index)
            root.mkdir()
            self.git(root, 'init', '-q')
            (root / 'code.txt').write_text('initial')
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-qm', 'initial')
            self.git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
            self.git(root, 'config', 'branch.' + self.git(root, 'branch', '--show-current') + '.remote', 'origin')
            self.git(root, 'config', 'branch.' + self.git(root, 'branch', '--show-current') + '.merge', 'refs/heads/main')
            self.git(root, 'config', 'remote.origin.url', 'https://example.com/demo/repo.git')
            self.git(root, 'config', 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
            self.manager.repo_top(str(root), record='start')
            self.repos.append(root)
            for offset in range(10):
                s = sessions.Session(project='demo', path=str(root), state='exited',
                                     created=1 if offset == 0 else time.time() + 86400)
                self.manager.sessions[s.id] = s
                self.rows.append(s)

    @staticmethod
    def git(root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE,
                                       text=True).strip()

    def refresh(self):
        with mock.patch.object(gitops, 'file_status', wraps=gitops.file_status) as status, \
                mock.patch.object(self.manager, '_session_top', wraps=self.manager._session_top) as trust:
            rows = {r['id']: r for r in self.manager._board()}
        return rows, status.call_count, trust.call_count

    def test_only_requested_git_previews_refresh_but_all_search_text_stays_available(self):
        with mock.patch.object(self.manager, 'conversation', return_value={
                'turns': [{'role': 'assistant', 'text': 'Searchable historical answer'}], 'usage': None}), \
                mock.patch.object(self.manager, '_session_top', wraps=self.manager._session_top) as trust, \
                mock.patch.object(gitops, 'file_status', wraps=gitops.file_status) as status:
            rows = self.manager._board(git_sessions={self.rows[0].id})
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(r['last'] == 'Searchable historical answer' for r in rows))
        self.assertEqual([c.args[0].id for c in trust.call_args_list], [self.rows[0].id])
        self.assertEqual(status.call_count, 1)
        self.assertEqual(set(self.manager._git_count), {self.rows[0].id})
        self.assertIsNone(rows[10]['changed'])
        self.assertEqual(rows[10]['flow']['commits'], 0)

    def test_deferred_git_cache_does_not_slide_and_refreshes_when_shown(self):
        before, _, _ = self.refresh()
        for s in self.rows:
            self.manager._git_count[s.id] = (0, self.manager._git_count[s.id][1])
            self.manager._flow_git[s.id] = (0, self.manager._flow_git[s.id][1])
        (self.repos[0] / 'new.txt').write_text('new')
        (self.repos[1] / 'new.txt').write_text('new')
        for workflow in (True, False):
            with self.subTest(workflow=workflow), mock.patch('features.on', side_effect=lambda name: workflow if name == 'workflow' else True):
                with mock.patch.object(self.manager, '_session_top', wraps=self.manager._session_top) as trust:
                    rows = {r['id']: r for r in self.manager._board(git_sessions={self.rows[0].id})}
                self.assertEqual(rows[self.rows[10].id]['changed'], before[self.rows[10].id]['changed'])
                self.assertEqual(self.manager._git_count[self.rows[10].id][0], 0)
                self.assertEqual(self.manager._flow_git[self.rows[10].id][0], 0)
                self.assertNotIn(self.rows[10].id, [c.args[0].id for c in trust.call_args_list])
        rows = {r['id']: r for r in self.manager._board(git_sessions={self.rows[10].id})}
        self.assertEqual(rows[self.rows[10].id]['changed'], 1)
        self.assertGreater(self.manager._git_count[self.rows[10].id][0], 0)

    def test_background_git_selection_unions_windows_expires_and_follows_changes_during_refresh(self):
        manager = self.manager
        manager._background_enabled = True
        queued = []
        clock = [100.0]
        with mock.patch.object(manager, '_start_service', side_effect=lambda fn, name: queued.append(fn) or True), \
                mock.patch.object(time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(manager, '_board', wraps=manager._board) as build:
            manager.board(git_sessions={self.rows[0].id, 'unknown'})
            self.assertEqual(len(queued), 1)
            queued.pop(0)()
            self.assertEqual(build.call_args.kwargs['git_sessions'], {self.rows[0].id})
            clock[0] += 1
            manager.board(git_sessions={self.rows[10].id})
            self.assertEqual(len(queued), 1, 'a newly shown session starts a refresh before the ordinary deadline')
            queued.pop(0)()
            self.assertEqual(build.call_args.kwargs['git_sessions'], {self.rows[0].id, self.rows[10].id})
            clock[0] += 16
            manager.board(git_sessions={self.rows[10].id})
            queued.pop(0)()
            self.assertEqual(build.call_args.kwargs['git_sessions'], {self.rows[10].id})
            self.rows[10].hidden = True
            clock[0] += 6
            manager.board(git_sessions=set())
            queued.pop(0)()
            self.assertEqual(build.call_args.kwargs['git_sessions'], set())
            self.assertNotIn(self.rows[10].id, manager._board_requested)
            # The selection changed during an in-flight build: it is not lost until the five-second deadline.
            clock[0] += 6
            manager.board(git_sessions={self.rows[0].id})
            def change_during_build(**kwargs):
                manager.board(git_sessions={self.rows[1].id})
                return []
            with mock.patch.object(manager, '_board', side_effect=change_during_build):
                queued.pop(0)()
            manager.board(git_sessions={self.rows[1].id})
            self.assertEqual(len(queued), 1)
            queued.pop(0)()
            self.assertIn(self.rows[1].id, build.call_args.kwargs['git_sessions'])

    def test_board_shares_repository_status_without_merging_session_commit_counts(self):
        rows, count, trust = self.refresh()
        self.assertEqual(count, 2, 'one status per repository, shared by counts and workflow')
        self.assertGreaterEqual(trust, len(self.rows), 'each session still validates its trusted root')
        for index in range(2):
            old, future = self.rows[index * 10:index * 10 + 2]
            self.assertEqual(rows[old.id]['flow']['commits'], 1)
            self.assertEqual(rows[future.id]['flow']['commits'], 0)
            self.assertTrue(rows[old.id]['flow']['pushed'])
            self.assertEqual(rows[old.id]['flow']['ahead'], 0)

    def test_board_count_and_workflow_use_one_validated_session_snapshot_without_sliding_ttl(self):
        rows, count, trust = self.refresh()
        self.assertEqual(trust, len(self.rows), 'one validated status snapshot supplies both fields of each card')
        self.assertEqual(count, 2)
        stamps = {s.id: self.manager._git_count[s.id][0] for s in self.rows}
        warm, count, trust = self.refresh()
        self.assertEqual(rows, warm)
        self.assertEqual((count, trust), (0, 0))
        self.assertEqual(stamps, {s.id: self.manager._git_count[s.id][0] for s in self.rows})
        (self.repos[0] / 'new.txt').write_text('new')
        for s in self.rows:
            self.manager._git_count[s.id] = (0, self.manager._git_count[s.id][1])
            self.manager._flow_git[s.id] = (0, self.manager._flow_git[s.id][1])
        current, count, trust = self.refresh()
        self.assertEqual((count, trust), (2, len(self.rows)))
        self.assertEqual(current[self.rows[0].id]['changed'], 1)
        self.assertTrue(current[self.rows[0].id]['flow']['impl'])

    def test_board_does_not_rediscover_validated_roots_or_read_unused_git_operations(self):
        with mock.patch.object(gitops, '_git', wraps=gitops._git) as calls:
            rows, _, trust = self.refresh()
        self.assertEqual(trust, len(self.rows), 'every session must still validate its repository')
        redundant = [c.args[1:] for c in calls.call_args_list if c.args[1:] in (
            ('rev-parse', '--show-toplevel'), ('rev-parse', '--absolute-git-dir'))]
        self.assertEqual(redundant, [], 'cards use file status of the root already validated for this session')
        self.assertEqual(rows[self.rows[0].id]['flow']['commits'], 1)
        self.assertEqual(rows[self.rows[1].id]['flow']['commits'], 0)

    def test_workflow_count_does_not_reuse_another_sessions_trusted_repository(self):
        original = self.manager._session_top
        excluded = self.rows[0]
        with mock.patch.object(self.manager, '_session_top', side_effect=lambda s: None if s is excluded else original(s)):
            rows = {r['id']: r for r in self.manager._board()}
        self.assertIsNone(rows[excluded.id]['changed'])
        self.assertEqual(rows[excluded.id]['flow']['commits'], 0)
        self.assertFalse(rows[excluded.id]['flow']['impl'])
        self.assertIsNotNone(rows[self.rows[1].id]['changed'])

    def test_expired_refresh_reads_new_status_and_head_without_retaining_shared_data(self):
        self.refresh()
        (self.repos[0] / 'code.txt').write_text('changed')
        self.git(self.repos[0], 'commit', '-qam', 'second')
        (self.repos[1] / 'new.txt').write_text('new')
        self.manager._git_count.clear()
        self.manager._flow_git.clear()
        rows, count, _ = self.refresh()
        self.assertEqual(count, 2)
        self.assertEqual(rows[self.rows[0].id]['flow']['commits'], 2)
        self.assertEqual(rows[self.rows[0].id]['flow']['ahead'], 1)
        self.assertFalse(rows[self.rows[0].id]['flow']['pushed'])
        self.assertEqual(rows[self.rows[10].id]['changed'], 1)
        self.assertTrue(rows[self.rows[10].id]['flow']['impl'])

    def test_workflow_disabled_still_shares_counts_and_untrusted_session_stays_empty(self):
        untrusted = sessions.Session(project='missing', path=str(Path(self.tmp.name, 'missing')), state='exited')
        self.manager.sessions[untrusted.id] = untrusted
        with mock.patch('features.on', side_effect=lambda name: name != 'workflow'):
            rows, count, _ = self.refresh()
        self.assertEqual(count, 2)
        self.assertIsNone(rows[untrusted.id]['changed'])
        self.assertIsNone(rows[untrusted.id]['flow'])

    def test_deleted_upstream_is_unknown_rather_than_reported_as_pushed(self):
        self.git(self.repos[0], 'update-ref', '-d', 'refs/remotes/origin/main')
        rows, count, _ = self.refresh()
        self.assertEqual(count, 2)
        self.assertIsNone(rows[self.rows[0].id]['flow']['ahead'])
        self.assertFalse(rows[self.rows[0].id]['flow']['pushed'])

    def test_state_list_reads_shared_branches_and_grants_once_per_snapshot_and_sees_changes(self):
        grant = {'scope': 'demo', 'project': 'demo', 'expires_at': time.time() + 600}
        common.write_json(sessions.APPROVAL_GRANTS_FILE, [grant])
        with mock.patch.object(gitops, 'head_branch', wraps=gitops.head_branch) as head, \
                mock.patch.object(sessions, 'read_json', wraps=sessions.read_json) as read:
            rows = self.manager.list()
        self.assertEqual(head.call_count, 2)
        self.assertEqual(sum(c.args[0] == sessions.APPROVAL_GRANTS_FILE for c in read.call_args_list), 1)
        self.assertTrue(all(r['approval_grants'] == [grant] for r in rows))
        common.write_json(sessions.APPROVAL_GRANTS_FILE, [])
        self.git(self.repos[0], 'branch', '-m', 'renamed')
        rows = {r['id']: r for r in self.manager.list()}
        self.assertEqual(rows[self.rows[0].id]['branch'], 'renamed')
        self.assertTrue(all(not r['approval_grants'] for r in rows.values()))

    def test_hidden_rule_resolves_each_session_path_once_and_keeps_every_pinned_exclude(self):
        import os
        for i, s in enumerate(self.rows):
            s.excludes = ['private-%d.txt' % i]
        project = {'path': str(self.repos[0]), 'profile': None}
        with mock.patch.object(os.path, 'realpath', wraps=os.path.realpath) as resolve:
            hidden = self.manager.project_hidden(project)
        for i in range(20):
            self.assertEqual(hidden('private-%d.txt' % i), i < 10)
        for root in self.repos:
            self.assertLessEqual(sum(c.args == (str(root),) for c in resolve.call_args_list), 1,
                                 'same-call path resolution must not scale with historical sessions')

    def test_hidden_rule_sees_retargeted_session_path_and_changed_excludes_on_next_read(self):
        alias = Path(self.tmp.name, 'alias')
        alias.symlink_to(self.repos[0], target_is_directory=True)
        s = sessions.Session(project='demo', path=str(alias), state='exited', excludes=['linked-secret.txt'])
        self.manager.sessions[s.id] = s
        project = {'path': str(self.repos[0]), 'profile': None}
        self.assertTrue(self.manager.project_hidden(project)('linked-secret.txt'))
        alias.unlink()
        alias.symlink_to(self.repos[1], target_is_directory=True)
        self.assertFalse(self.manager.project_hidden(project)('linked-secret.txt'))
        s.path = str(self.repos[0])
        s.excludes = ['new-secret.txt']
        hidden = self.manager.project_hidden(project)
        self.assertFalse(hidden('linked-secret.txt'))
        self.assertTrue(hidden('new-secret.txt'))


class ClaudeBoardPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(mock.patch.object(common, 'REAL_HOME', self.tmp.name))
        self.manager = _env.manager(self)
        self.cfg = Path(self.tmp.name, '.claude')
        for i in range(30):
            (self.cfg / 'projects' / ('project-%02d' % i)).mkdir(parents=True)
        self.rows = []
        for i in range(20):
            s = sessions.Session(kind='advisor', claude_mode='host', claude_session=str(uuid.uuid4()), state='exited')
            self.manager.sessions[s.id] = s
            self.rows.append(s)
            if i < 12:
                self.write(s, 'project-00', 'Answer %d' % i)

    def write(self, s, folder, text):
        p = self.cfg / 'projects' / folder / (s.claude_session + '.jsonl')
        p.write_text(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}) + '\n')
        return p

    def test_shared_claude_root_is_indexed_once_including_missing_histories_and_cold_parsers(self):
        with mock.patch.object(common, 'safe_listdir', wraps=common.safe_listdir) as scan:
            first = self.manager._board()
            cold = scan.call_count
            second = self.manager._board()
            warm = scan.call_count - cold
        self.assertEqual(first, second)
        self.assertEqual(first[0]['last'], 'Answer 0')
        self.assertLessEqual(cold, 32)
        self.assertLessEqual(warm, 32)
        self.write(self.rows[12], 'project-01', 'New history')
        self.assertEqual({r['id']: r for r in self.manager._board()}[self.rows[12].id]['last'], 'New history')

    def test_claude_index_preserves_latest_mtime_and_equal_mtime_directory_priority(self):
        s = self.rows[0]
        first = self.cfg / 'projects/project-00' / (s.claude_session + '.jsonl')
        second = self.write(s, 'project-01', 'Other answer')
        import os
        os.utime(first, ns=(1000000000, 1000000000))
        os.utime(second, ns=(1000000000, 1000000000))
        self.assertEqual(digest.history_file_map(str(self.cfg), 'claude')[s.claude_session],
                         digest.transcript(str(self.cfg), s.claude_session))
        os.utime(second, ns=(2000000000, 2000000000))
        self.assertEqual({r['id']: r for r in self.manager._board()}[s.id]['last'], 'Other answer')
        second.unlink()
        second.symlink_to(first)
        self.assertEqual({r['id']: r for r in self.manager._board()}[s.id]['last'], 'Answer 0')

    def test_board_retains_short_card_text_instead_of_full_conversation_pages(self):
        import gc
        import tracemalloc
        for s in self.rows[5:]:
            s.hidden = True
        def page(*args, **kwargs):
            return {'turns': [{'role': 'assistant' if i % 2 else 'user', 'text': str(i) + 'x' * 2048}
                              for i in range(400)]}
        gc.collect()
        tracemalloc.start()
        try:
            with mock.patch.object(digest, 'digest', side_effect=page), \
                    mock.patch.object(digest, 'usage', return_value=None):
                rows = self.manager._board()
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(rows[0]['last']), 700)
        self.assertLess(retained, 1 << 20, 'five card summaries must not keep five full 400-message pages alive')


class GitGuardWorktreePerformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = _env.manager(self)
        self.main = str(Path(self.tmp.name, 'main').resolve())
        self.git('init', '-q', self.main)
        self.git('-C', self.main, 'commit', '-qm', 'initial', '--allow-empty')
        self.recs = [{'common': self.main + '/.git', 'gitdir': self.main + '/.git'}]
        for i in range(2):
            path = str(Path(self.tmp.name, 'worktree-' + str(i)).resolve())
            self.git('-C', self.main, 'worktree', 'add', '-qb', 'branch-' + str(i), path)
            gitdir = self.git('-C', path, 'rev-parse', '--absolute-git-dir').strip()
            self.recs.append({'common': self.main + '/.git', 'gitdir': gitdir})

    def git(self, *args):
        return subprocess.check_output(['git', *args], text=True, stderr=subprocess.PIPE)

    def test_alternating_worktrees_do_not_reparse_or_persist_unchanged_guard_state(self):
        for mode in ('hole', 'guarded'):
            with self.subTest(mode=mode):
                self.manager.policy['git_mode'] = mode
                for rec in self.recs:
                    self.assertTrue(self.manager._git_guard(rec))
                with mock.patch.object(sessions, 'write_json', wraps=sessions.write_json) as save, \
                        mock.patch.object(gitops, 'guard_state', wraps=gitops.guard_state) as read:
                    for _ in range(5):
                        for rec in self.recs:
                            self.assertTrue(self.manager._git_guard(rec))
                self.assertEqual(read.call_count, 0, 'unchanged worktrees must keep independent signatures')
                self.assertEqual(save.call_count, 0, 'alternating worktrees must not rewrite the baseline')

    def test_worktree_baselines_survive_other_worktrees_and_changes_are_checked_immediately(self):
        self.manager.policy['git_mode'] = 'hole'
        first, second = self.recs[1:]
        config = first['gitdir'] + '/config.worktree'
        self.git('config', '--file', config, 'core.pager', 'less')
        for rec in self.recs:
            self.assertTrue(self.manager._git_guard(rec))
        writer = sessions.Session(path=self.main, state='ready', pid=4321)
        self.manager.policy['git_mode'] = 'guarded'
        with mock.patch.object(self.manager, '_guard_writers', return_value=[writer]):
            # Another worktree has not discarded this one's accepted baseline.
            self.assertTrue(self.manager._git_guard(first))
            self.assertEqual(writer.state, 'ready')
            self.assertEqual(self.git('config', '--file', config, 'core.pager').strip(), 'less')
            self.git('config', '--file', config, 'core.fsmonitor', 'unexpected-command')
            self.assertTrue(self.manager._git_guard(second))
            self.assertTrue(self.manager._git_guard(first))
            self.assertEqual(writer.state, 'attention')
            keys = self.git('config', '--file', config, '--list')
            self.assertNotIn('fsmonitor', keys)
            self.assertIn('core.pager=less', keys)
            writer.state = 'ready'
            hook = Path(first['common'], 'hooks/post-checkout')
            hook.write_text('#!/bin/sh\nexit 0\n')
            self.assertTrue(self.manager._git_guard(second))
            self.assertFalse(hook.exists(), 'shared hook changes must invalidate every worktree signature')
            self.assertEqual(writer.state, 'attention')
            self.assertTrue(self.manager._git_guard(first))


class ConversationCachePerformanceTest(unittest.TestCase):
    # The same shared-history fixture exercises actual conversation lookup and
    # versioning; only the expensive page construction is controlled here.
    setUp = ClaudeBoardPerformanceTest.setUp
    write = ClaudeBoardPerformanceTest.write

    def test_opening_many_conversations_retains_bounded_pages_for_both_agents(self):
        import gc
        import tracemalloc
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                for s in self.rows:
                    s.agent = agent
                def page(*args, **kwargs):
                    return {'turns': [{'role': 'assistant', 'text': str(i) + 'x' * 2048}
                                      for i in range(400)]}
                gc.collect()
                tracemalloc.start()
                try:
                    with mock.patch.object(digest, 'digest', side_effect=page), \
                            mock.patch.object(digest, 'codex_digest', side_effect=page), \
                            mock.patch.object(digest, 'usage', return_value=None):
                        for s in self.rows:
                            result = self.manager.conversation(s.id)
                            self.assertEqual(len(result['turns']), 400)
                    del result
                    gc.collect()
                    retained = tracemalloc.get_traced_memory()[0]
                finally:
                    tracemalloc.stop()
                self.assertLessEqual(len(self.manager._conv_cache), 8)
                self.assertLess(retained, 12 << 20, 'visited pages must not accumulate for the lifetime of the daemon')

    def test_cache_reuses_recent_pages_evicts_old_pages_and_returns_oversized_pages_uncached(self):
        def page(*args, **kwargs):
            return {'turns': [{'role': 'assistant', 'text': 'answer'}]}
        with mock.patch.object(digest, 'digest', side_effect=page) as parse, \
                mock.patch.object(digest, 'usage', return_value=None):
            for s in self.rows[:8]:
                self.manager.conversation(s.id)
            first = self.manager.conversation(self.rows[0].id)
            self.assertEqual(parse.call_count, 8)
            self.manager.conversation(self.rows[8].id)
            self.assertIs(self.manager.conversation(self.rows[0].id), first)
            self.assertEqual(parse.call_count, 9)
            self.manager.conversation(self.rows[1].id)
            self.assertEqual(parse.call_count, 10)
        huge = {'turns': [{'role': 'assistant', 'text': 'x' * (17 << 20)}]}
        with mock.patch.object(digest, 'digest', return_value=huge), \
                mock.patch.object(digest, 'usage', return_value=None):
            result = self.manager.conversation(self.rows[0].id, turns=2000)
        self.assertIs(result['turns'], huge['turns'])
        self.assertNotIn(self.rows[0].id, self.manager._conv_cache)

    def test_cache_memory_budget_counts_unicode_storage_across_pages(self):
        import sys
        def page(*args, **kwargs):
            return {'turns': [{'role': 'assistant', 'text': '\U00010348' * (1 << 20)}]}
        with mock.patch.object(digest, 'digest', side_effect=page), \
                mock.patch.object(digest, 'usage', return_value=None):
            for s in self.rows[:7]:
                result = self.manager.conversation(s.id)
                self.assertEqual(len(result['turns'][0]['text']), 1 << 20)
        retained_text = sum(sys.getsizeof(t['text']) for _, r in self.manager._conv_cache.values()
                            for t in r['turns'])
        self.assertLess(retained_text, 16 << 20, 'a count limit alone is insufficient for large Unicode pages')
        self.assertEqual(len(self.manager._conv_cache), 3)


class ReverseRecordsTest(unittest.TestCase):
    def test_reverse_matches_complete_forward_records_across_chunks_and_partial_tail(self):
        with tempfile.TemporaryDirectory() as root:
            rows = [{'n': i, 'text': ('x' if i % 2 else '\u011b') * size} for i, size in enumerate((1, 8190, 16000, 200000, 10))]
            data = '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n{"unfinished":'
            p = Path(root, 'records.jsonl')
            p.write_text(data)
            expected = [r for _, r in digest.iter_records(root, p.name) if r]
            self.assertEqual(list(digest.reverse_records(root, p.name, p.stat().st_size)), list(reversed(expected)))

    def test_reverse_skips_oversized_malformed_and_nonobject_records_without_losing_neighbors(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root, 'records.jsonl')
            p.write_text('{"n":1}\n' + json.dumps({'huge': 'x' * (10 << 20)}) + '\ninvalid\n[]\n{"n":2}\n')
            self.assertEqual(list(digest.reverse_records(root, p.name, p.stat().st_size)), [{'n': 2}, {'n': 1}])
