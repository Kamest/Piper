"""Resumable, laptop-driven server setup over remotes' authenticated one-shot SSH peer.

API (full local GUI token): GET /api/environments/<id>/setup returns snapshot only;
POST .../setup {projects?: [local project key]} starts/rechecks automatic steps (202).
POST .../setup/<step> fixes one step (202); {reconcile: true} checks an unknown outcome
without repeating a write. Only after that check may {retry_unknown: true} explicitly retry.
Step ids: service, hole, git, extensions, tools, projects, configuration, agents, secrets, phone.
The last three deferred steps (configuration, secrets, phone) remain todo without actions in S1.
No request bodies, archives, argv, command output or secret values are persisted here.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

import common
import extensions
import projects
import remotes

def forge_host(url):
    m = re.fullmatch(r'(?:ssh://)?[A-Za-z0-9_.-]+@([A-Za-z0-9_.-]+)(?::[0-9]{1,5}/|:|/).*', url)
    return m[1] if m else None


def known_hosts(urls):
    """This computer's pinned host keys for the forges (public data); hashed entries stay hashed."""
    import subprocess
    lines = []
    for host in sorted({forge_host(u) for u in urls if forge_host(u)}):
        try:
            reply = subprocess.run(['ssh-keygen', '-F', host], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        lines += [x.strip() for x in reply.stdout.splitlines() if x.strip() and not x.startswith('#')]
    import remotepeer
    return [x for x in dict.fromkeys(lines) if remotepeer.HOST_KEY_LINE.fullmatch(x)][:50]


STEPS = [('service', 'Piper service'), ('hole', 'Hole'), ('git', 'Git access'),
         ('extensions', 'Extensions'), ('tools', 'Tools from extensions'), ('projects', 'Projects'),
         ('configuration', 'Configuration'), ('agents', 'Agent sign-in'), ('secrets', 'Secrets'),
         ('phone', 'Phone backup')]
DEFERRED = {'configuration', 'secrets', 'phone'}
AUTOMATIC = [key for key, _ in STEPS if key not in DEFERRED]


class UnknownOutcome(RuntimeError):
    pass


class Setup:
    """One worker per environment; every write has a durable intent before it leaves the laptop."""

    def __init__(self, manager=None, home=None):
        self.manager = manager
        self.root = Path(home or common.PIPER_HOME) / 'server-setup'
        self.lock = threading.RLock()
        self.records = {}
        self.threads = {}
        self.closed = False

    def _path(self, eid):
        if not isinstance(eid, str) or not remotes.ID.fullmatch(eid) or eid == 'local':
            raise ValueError('invalid setup environment')
        return self.root / eid / 'setup.json'

    def _load(self, eid):
        path = self._path(eid)
        if eid not in self.records:
            if path.exists():
                record = common.read_json(str(path), None)
                if not isinstance(record, dict) or record.get('version') != 1 or record.get('environment') != eid:
                    raise ValueError('Invalid setup record; inspect local setup state before continuing.')
            else:
                record = {'version': 1, 'environment': eid, 'status': 'idle', 'phase': 'requested',
                          'updated': None, 'steps': [dict(id=key, number=n, title=title, state='todo',
                          detail='Planned in a later setup session.' if key in DEFERRED else 'Not checked yet.')
                          for n, (key, title) in enumerate(STEPS, 1)], 'operations': {}, 'inflight': None,
                          'projects': [], 'path_map': [], 'requirements': [], 'secrets': []}
            self.records[eid] = record
        return self.records[eid]

    def _save(self, record):
        path = self._path(record['environment'])
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record['updated'] = time.time()
        common.write_json(str(path), record)
        # Before dispatching a mutation, intent must survive a daemon restart.
        with path.open('rb') as saved:
            os.fsync(saved.fileno())

    @staticmethod
    def _row(record, step):
        return next(row for row in record['steps'] if row['id'] == step)

    def _set(self, record, step, state, detail, action=None, **extra):
        with self.lock:
            row = self._row(record, step)
            row.update(state=state, detail=detail, **extra)
            row.pop('action', None)
            if action:
                row['action'] = action
            self._save(record)

    @staticmethod
    def _action(record, step, kind='fix', body=None):
        return {'kind': kind, 'method': 'POST', 'path': '/api/environments/%s/setup/%s' %
                (record['environment'], step), 'body': body or {}}

    def state(self, eid):
        remotes.get(eid)
        with self.lock:
            record = self._load(eid)
            return copy.deepcopy({k: v for k, v in record.items() if k not in
                                  ('operations', 'inflight', 'selection', 'targets', 'requested_projects', 'reconcile', 'git_identity')})

    def resume(self):
        """Called once at daemon start: finished phases survive; in-flight writes become unknown."""
        if not self.root.exists():
            return
        for path in self.root.glob('*/setup.json'):
            try:
                remotes.get(path.parent.name)
                with self.lock:
                    record = self._load(path.parent.name)
                    if record['status'] != 'running':
                        continue
                    if record.get('inflight'):
                        self._unknown(record)
                    else:
                        self._spawn(record)
            except (ValueError, OSError):
                continue  # unknown/removed environment or damaged record: never dispatch it

    def _selected_projects(self, keys):
        if keys is None:
            rows = self.manager.list() if self.manager else []
            keys = sorted({s['project'].partition('@')[0] for s in rows if s.get('project') and not s.get('moved')
                           and not s.get('environment') and s.get('state') not in ('exited', 'error', 'blocked')})
        if not isinstance(keys, list) or len(keys) > 100 or not all(isinstance(k, str) for k in keys):
            raise ValueError('projects must be a list of local project keys')
        root = Path(os.environ.get('PROJECT_PATH', '')).resolve()
        targets = []
        for key in dict.fromkeys(keys):
            project = projects.find(key)
            if not project or not project['exists'] or '@' in key:
                raise ValueError('Select existing main project checkouts for setup.')
            path = Path(project['path']).resolve()
            if not path.is_relative_to(root) or path == root:
                raise ValueError('Projects must be inside the local projects root to mirror their layout.')
            url = projects.origin_url(str(path)) or ''
            if url.startswith('https://'):
                parts = urlsplit(url)
                if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
                    raise ValueError('Project origin must not contain credentials or query parameters.')
                if parts.port not in (None, 443):
                    raise ValueError('Use a credential-free SSH origin for a forge on a custom HTTPS port.')
                url = 'git@' + parts.hostname + ':' + parts.path.lstrip('/')
            # Validate the exact peer contract locally before a worker or write is started.
            import remotepeer
            relative = path.relative_to(root).as_posix()
            remotepeer._clone_target({'project_path': str(remotepeer.HOME / 'projects'),
                                      'relative_path': relative, 'url': url})
            targets.append(dict(key=key, local_path=str(path), relative_path=relative, url=url))
        return targets

    def start(self, eid, body=None, step=None):
        body = body or {}
        if not isinstance(body, dict) or set(body) - {'projects', 'reconcile', 'retry_unknown'}:
            raise ValueError('unknown setup fields')
        for flag in ('reconcile', 'retry_unknown'):
            if flag in body and type(body[flag]) is not bool:
                raise ValueError(flag + ' must be boolean')
        if body.get('reconcile') and body.get('retry_unknown'):
            raise ValueError('reconcile and retry are separate actions')
        if step is not None and step not in AUTOMATIC:
            raise ValueError('unknown or deferred setup step')
        if 'projects' in body and (not isinstance(body['projects'], list) or len(body['projects']) > 100 or not all(
                isinstance(k, str) and projects.KEY_RX.fullmatch(k) and '@' not in k for k in body['projects'])):
            raise ValueError('projects must be a list of local main project keys')
        if 'projects' in body and step not in (None, 'projects'):
            raise ValueError('project selection belongs to setup or the projects step')
        entry = remotes.get(eid)
        if entry['mode'] != 'service':
            raise ValueError('server setup needs service mode')
        with self.lock:
            record = self._load(eid)
            thread = self.threads.get(eid)
            if thread and thread.is_alive():
                return self.state(eid)  # duplicate POST does not start another worker
            if record.get('inflight') and not record.get('unknown'):
                self._unknown(record)
            unknown = record.get('unknown')
            if unknown:
                if 'projects' in body:
                    raise ValueError('Project selection cannot change while an outcome is unknown.')
                if step != unknown['step']:
                    raise ValueError('Check the unknown server outcome using its step action first.')
                if body.get('reconcile'):
                    record['selection'] = [step]
                    record['reconcile'] = True
                elif body.get('retry_unknown') and unknown.get('checked'):
                    record.pop('unknown', None)
                    record['inflight'] = None
                    record['operations'].pop(unknown['operation'], None)
                    record['reconcile'] = False
                else:
                    raise ValueError('Reconcile the unknown server outcome before explicitly retrying it.')
            elif body.get('reconcile') or body.get('retry_unknown'):
                raise ValueError('There is no unknown setup outcome to reconcile or retry.')
            if not unknown:
                if 'projects' in body or 'targets' not in record:
                    # Discovery and git reads belong to the worker, never the GUI request path.
                    record['targets'] = None
                    record['requested_projects'] = body.get('projects')
                    record['projects'] = list(body.get('projects') or [])
                # A new explicit pass rechecks reality; only recovery retains its completed phases.
                record['operations'] = {}
            record['selection'] = [step] if step else list(AUTOMATIC)
            record['status'], record['phase'] = 'running', 'requested'
            self._save(record)
            reply = self.state(eid)
            self._spawn(record)
            return reply

    def _spawn(self, record):
        if self.closed:
            return
        existing = self.threads.get(record['environment'])
        if existing is not None and existing.is_alive():
            return
        th = threading.Thread(target=self._run, args=(record,), name='server-setup-' + record['environment'], daemon=True)
        self.threads[record['environment']] = th
        th.start()

    def _unknown(self, record):
        inflight = record['inflight']
        step = inflight['step']
        record['unknown'] = {'step': step, 'operation': inflight['key'], 'checked': False}
        record['status'], record['phase'] = 'halted', 'remote_unknown'
        self._set(record, step, 'needs_user', 'The server answer was lost. Check the result before retrying.',
                  self._action(record, step, 'reconcile', {'reconcile': True}))

    def _write(self, record, step, key, call):
        """The write-ahead phase record is the authority, just like transfer.json."""
        with self.lock:
            if record.get('unknown') or record.get('inflight'):
                raise UnknownOutcome('Resolve the previous server write before another operation.')
            if key in record['operations']:
                return copy.deepcopy(record['operations'][key])
            record['inflight'] = {'step': step, 'key': key}
            record['phase'] = 'remote_sending'
            self._save(record)
        try:
            result = call()
            if not isinstance(result, dict):
                raise UnknownOutcome()
            # Only public metadata may enter the phase record, never payloads or command output.
            result = {k: v for k, v in result.items() if k in
                      ('installed', 'public_key', 'sha', 'extensions', 'returncode', 'timed_out', 'path', 'existing')}
        except remotes.Rejected:
            with self.lock:
                record['inflight'] = None
                record['phase'] = step
                self._save(record)
            raise
        except Exception:
            with self.lock:
                self._unknown(record)
            raise UnknownOutcome() from None
        with self.lock:
            inflight = record['inflight']
            record['operations'][key] = result
            record['inflight'] = None
            record['phase'] = step
            try:
                self._save(record)
            except OSError:
                # The peer answered, but completion was not durable. Keep the intent in memory too;
                # an explicit POST in this process must not replay it before reconciliation.
                record['operations'].pop(key, None)
                record['inflight'] = inflight
                try:
                    self._unknown(record)
                except OSError:
                    pass
                raise UnknownOutcome() from None
        return result

    def _peer(self, record, op, body=None):
        return remotes.setup_request(record['environment'], op, body)

    def _run(self, record):
        try:
            if record.get('reconcile'):
                self._reconcile(record)
                return
            for step in record['selection']:
                if self.closed:
                    return
                self._set(record, step, 'running', 'Checking server…')
                try:
                    self._step(record, step)
                except UnknownOutcome:
                    return
                except remotes.Rejected as error:
                    self._set(record, step, 'needs_user', common.scrub(str(error))[:500], self._action(record, step))
                except OSError:
                    return  # a local phase record could not be written: never dispatch another write
                except Exception:
                    self._set(record, step, 'failed', 'Setup step failed. Inspect server prerequisites and try Fix.',
                              self._action(record, step))
            with self.lock:
                record['phase'] = 'done'
                record['status'] = 'halted' if any(self._row(record, key)['state'] != 'ok'
                                                  for key in record['selection']) else 'done'
                self._save(record)
        except UnknownOutcome:
            pass
        except Exception:
            with self.lock:
                record['status'], record['phase'] = 'halted', 'check_failed'
                self._save(record)

    def _requirements(self, record, status):
        server = {k: status.get(k) for k in ('home', 'platform', 'python', 'docker', 'hole', 'projects')}
        server.update({k: remotes.get(record['environment'])[k] for k in ('id', 'project_path', 'mode')})
        rows = extensions.server_requirements(server)
        with self.lock:
            record['requirements'] = [{k: r[k] for k in ('id', 'title', 'detail', 'secrets')} for r in rows]
            record['secrets'] = [dict(s, requirement=r['id']) for r in rows for s in r['secrets']]
            self._save(record)
        return rows

    def _step(self, record, step, *, check_only=False):
        if step in ('git', 'projects') and record.get('targets') is None:
            targets = self._selected_projects(record.get('requested_projects'))
            with self.lock:
                record['targets'] = targets
                record['projects'] = [p['key'] for p in targets]
                self._save(record)
        status = self._peer(record, 'setup_status', {'project_checks':
                            [p['relative_path'] for p in (record.get('targets') or [])]})
        fix = self._action(record, step)
        def result(ok, detail, **extra):
            self._set(record, step, 'ok' if ok else 'needs_user', detail, None if ok else fix, **extra)
            return ok
        def write(op, body=None, key=None):
            if check_only:
                return None
            return self._write(record, step, key or step, lambda: self._peer(record, op, body))
        if step == 'service':
            raw, sha = remotes.bundle()
            ok = status.get('service_running') and status.get('project_root_matches') and status.get('bundle_sha') == sha
            if not ok and not check_only:
                self._write(record, step, step, lambda: remotes.request(remotes.get(record['environment']),
                            {'op': 'install', 'setup': True, 'archive': base64.b64encode(raw).decode(), 'sha': sha}, timeout=120))
                status = self._peer(record, 'setup_status')
                ok = status.get('service_running') and status.get('project_root_matches') and status.get('bundle_sha') == sha
            return result(bool(ok), 'Service is current.' if ok else 'Install/update could not be verified; inspect the user service.')
        if step == 'hole':
            if not (status.get('docker') or {}).get('usable'):
                return result(False, 'Install or enable Docker Engine for this account on the server, then Fix. Piper never runs sudo.')
            hole = status.get('hole') or {}
            ok = bool(hole.get('current'))
            if not ok and not check_only:
                write('install_hole')
                hole = self._peer(record, 'setup_status').get('hole') or {}
                ok = bool(hole.get('current'))
            # Hole builds its images on the first sandboxed session; setup never starts an agent.
            return result(ok, ('Hole %s is installed.' % hole.get('version')) + ('' if hole.get('image') else
                          ' Its image is built on the first sandboxed session.') if ok else
                          'Hole could not be installed or updated; inspect it on the server.')
        if step == 'git':
            public = (status.get('git_key') or {}).get('public_key')
            urls = sorted({p['url'] for p in record.get('targets') or []})
            def check():
                # Host keys come from this computer's known_hosts (public, already trusted here);
                # appending them and ls-remote are idempotent, so this is a check, not a tracked write.
                return self._peer(record, 'git_access', {'urls': urls, 'known_hosts': known_hosts(urls)}).get('access') or {}
            access = check() if urls and not check_only else {}
            ok = bool(urls) and all(access.get(u) for u in urls)
            if not ok and not public and not check_only:
                # Only when the account's own SSH setup cannot read every repository.
                public = (write('setup_git_key') or {}).get('public_key')
                access = check() if urls else {}
                ok = bool(urls) and all(access.get(u) for u in urls)
            with self.lock:
                record['git_identity'] = {u: access.get(u) for u in urls if access.get(u)}
            if ok:
                self._set(record, step, 'ok', 'The server can read the selected repositories.', None,
                          public_key=public, access={u: access[u] for u in urls})
                return True
            if not public:
                return result(False, 'The server key could not be created; inspect ~/.ssh on the server.')
            hosts = sorted({forge_host(u) for u in urls if forge_host(u)})
            # No generic account/scope-aware forge key registration exists. Never export a laptop token.
            self._set(record, step, 'ok' if ok else 'needs_user',
                      'The server can read the selected repositories.' if ok else
                      ('Add this public key to your forge account (SSH keys) or as a deploy key, then Fix.' if urls else
                       'Select projects first; then add this public key to your forge account and Fix.'),
                      None if ok else {'kind': 'public_key', 'public_key': public, 'hosts': hosts,
                                       'fix': self._action(record, step)},
                      public_key=public, access={u: access.get(u) for u in urls})
            return check_only  # reconciliation verifies key generation; forge access still needs the user
        if step == 'extensions':
            raw, sha, names = remotes.extension_bundle()
            ok = status.get('extensions_sha') == sha and sorted(status.get('extensions') or []) == sorted(names)
            if not ok and not check_only:
                write('sync_extensions', {'archive': base64.b64encode(raw).decode(), 'sha': sha})
                status = self._peer(record, 'setup_status')
                ok = status.get('extensions_sha') == sha and sorted(status.get('extensions') or []) == sorted(names)
            return result(ok, 'Trusted extensions are loaded.' if ok else 'Extension loading is not verified.')
        if step == 'tools':
            rows = self._requirements(record, status)
            failed = []
            for row in rows:
                body = {'argv': row['check'], 'timeout': 60}
                reply = self._peer(record, 'run_requirement', body)
                if reply.get('returncode') == 0:
                    continue
                if row['install'] is not None and not check_only:
                    digest = hashlib.sha256(json.dumps(row['install']).encode()).hexdigest()[:12]
                    write('run_requirement', {'argv': row['install'], 'timeout': 300}, 'tools:' + row['id'] + ':' + digest)
                    reply = self._peer(record, 'run_requirement', body)
                if reply.get('returncode') != 0:
                    failed.append(row['id'])
            return result(not failed, 'Extension requirements are satisfied.' if not failed else 'Requirements need attention: ' + ', '.join(failed))
        if step == 'projects':
            origins = status.get('project_origins') or {}
            missing, mapping = [], []
            entry = remotes.get(record['environment'])
            for project in record.get('targets', []):
                root = status.get('requested_project_root') or entry['project_path']
                target = root.rstrip('/') + '/' + project['relative_path']
                expected_origin = hashlib.sha256(project['url'].encode()).hexdigest()
                if origins.get(target) != expected_origin:
                    identity = (record.get('git_identity') or {}).get(project['url']) or 'setup'
                    reply = write('clone_project', dict({k: project[k] for k in ('relative_path', 'url')}, identity=identity),
                                  'projects:' + project['key'])
                    if reply:
                        target = reply['path']
                    else:
                        missing.append(project['key'])
                mapping.append(dict(project=project['key'], local_path=project['local_path'], remote_path=target))
            with self.lock:
                record['path_map'] = mapping
                self._save(record)
            return result(not missing, 'Selected projects are available.' if not missing else 'Some selected projects are missing.')
        if step == 'agents':
            login = status.get('agent_login') or {}
            ok = any(value == 'ok' for value in login.values())
            self._set(record, step, 'ok' if ok else 'needs_user',
                      'An agent is signed in.' if ok else 'Sign in on the server using the existing SSH terminal.',
                      None if ok else {'kind': 'terminal', 'environment': record['environment'],
                                       'commands': {'claude': ['claude', '/login'], 'codex': ['codex', 'login', '--device-auth']}},
                      agents=login)
            return ok
        raise ValueError('unknown setup step')

    def _reconcile(self, record):
        unknown = record['unknown']
        step = unknown['step']
        ok = self._step(record, step, check_only=True)
        with self.lock:
            record['reconcile'] = False
            record['inflight'] = None
            if ok:
                record.pop('unknown', None)
                record['status'], record['phase'] = 'done', 'reconciled'
            else:
                unknown['checked'] = True
                record['status'], record['phase'] = 'halted', 'remote_unknown'
                self._set(record, step, 'needs_user', 'The result could not be confirmed. Retry only after inspecting the server.',
                          self._action(record, step, 'retry', {'retry_unknown': True}))
            self._save(record)

    def close(self):
        self.closed = True


CURRENT = Setup()
