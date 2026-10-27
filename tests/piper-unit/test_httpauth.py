"""Managed auth runs against synthetic issuers; no real account or provider login."""
import base64
import json
import ssl
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import _env
import httpauth as auth
import httpclient


class TokensTest(unittest.TestCase):
    def test_routed_token_and_interactive_posts_choose_tls_context_for_their_own_endpoint(self):
        tokens = self.tokens()
        verified, tunnel = ssl.create_default_context(), ssl._create_unverified_context()
        tokens.route = lambda url:url.replace('issuer.example', 'localhost:14443')
        tokens.context_for = lambda url:tunnel if url.startswith('https://localhost:14443/') else verified
        calls = []
        def transport(url, parameters, headers, deadline, context, **kwargs):
            calls.append((url, context))
            return dict(self.response)
        tokens.transport = transport
        self.fetch(tokens)
        self.assertEqual(calls[-1], ('https://localhost:14443/token', tunnel))
        flow = object.__new__(auth.SignIn)
        flow.tokens, flow.deadline = tokens, time.monotonic() + 10
        try:
            for url, context in [('https://issuer.example/device', tunnel), ('https://other.example/token', verified)]:
                flow._post(url, [], {}, threading.Event())
                self.assertIs(calls[-1][1], context)
        finally:
            tokens.context_for = lambda url:tokens.context

    def setUp(self):
        self.secrets, self.calls = [], []
        self.response = dict(access_token='synthetic-access42', id_token='synthetic-id42', refresh_token='synthetic-refresh42', expires_in=3600)
        self.config = {'Type':'OAuth2', 'Grant Type':'Client Credentials', 'Client ID':'client +:/42',
                       'Client Secret':'secret +:/42', 'Token URL':'https://issuer.example/token'}

    def transport(self, url, parameters, headers, deadline, context):
        self.calls.append((url, parameters, headers))
        return dict(self.response)

    def tokens(self, config=None):
        return auth.Tokens({'main':config or self.config}, self.secrets, self.transport)

    def fetch(self, tokens, kind='access', name='main', deadline=None):
        return tokens(dict(kind='auth', id=name, token=kind), deadline or time.monotonic() + 10)['token']

    def test_config_merge_is_per_id_and_resolves_without_mutating_original_documents(self):
        public = {'dev':{'Security':{'Auth':{'one':{'Type':'Mock', 'Token':'{{token}}'}, 'two':{'Type':'Mock', 'Token':'public'}}}}}
        private = {'dev':{'Security':{'Auth':{'two':{'Token':'private'}}}}}
        configs = auth.configurations(public, private, 'dev', {'token':'resolved'}, httpclient.substitute)
        self.assertEqual(configs['one']['Token'], 'resolved')
        self.assertEqual(configs['two'], {'Type':'Mock', 'Token':'private'})
        self.assertEqual(public['dev']['Security']['Auth']['two']['Token'], 'public')

    def test_original_dynamic_argument_token_kinds_cache_and_secret_registration(self):
        tokens = self.tokens()
        scrub = httpclient.redactor(self.secrets)
        self.assertEqual(self.fetch(tokens), 'synthetic-access42')
        self.assertEqual(self.fetch(tokens, 'id'), 'synthetic-id42')
        self.assertEqual(len(self.calls), 1)
        for token in ('synthetic-access42', 'synthetic-id42', 'synthetic-refresh42'):
            self.assertEqual(scrub(token), httpclient.MASK)
        params = dict(self.calls[0][1])
        self.assertEqual(params, {'grant_type':'client_credentials'})
        expected = urllib.parse.quote_plus(self.config['Client ID']) + ':' + urllib.parse.quote_plus(self.config['Client Secret'])
        self.assertEqual(base64.b64decode(self.calls[0][2]['Authorization'][6:]).decode(), expected)

    def test_password_body_client_auth_arrays_and_request_specific_customization(self):
        config = dict(self.config, **{'Grant Type':'Password', 'Username':'demo-user', 'Password':'demo-password42',
            'Client Credentials':'in body', 'Scope':'profile openid',
            'Custom Request Parameters':{'audience':['api-one', 'api-two'], 'auth_only':{'Value':'skip', 'Use':'In Auth Request'},
                                         'token_only':{'Value':'yes', 'Use':'In Token Request'}},
            'Custom Request Headers':{'X-Requested-By':{'Value':'piper-test', 'Use':'Everywhere'}}})
        self.fetch(self.tokens(config))
        params, headers = self.calls[0][1:]
        self.assertEqual([v for k,v in params if k == 'audience'], ['api-one', 'api-two'])
        self.assertNotIn('auth_only', dict(params))
        self.assertEqual(dict(params)['token_only'], 'yes')
        self.assertEqual(dict(params)['password'], 'demo-password42')
        self.assertEqual(dict(params)['client_secret'], 'secret +:/42')
        self.assertEqual(headers['X-Requested-By'], 'piper-test')
        self.assertNotIn('Authorization', headers)

    def test_refresh_uses_frozen_credentials_rotates_refresh_and_never_replays_resource(self):
        self.response['expires_in'] = 0
        tokens = self.tokens()
        self.fetch(tokens)
        self.response.update(access_token='synthetic-access43', refresh_token='synthetic-refresh43', expires_in=100)
        self.assertEqual(self.fetch(tokens), 'synthetic-access43')
        self.assertEqual(dict(self.calls[1][1]), {'grant_type':'refresh_token', 'refresh_token':'synthetic-refresh42'})
        self.assertIn('synthetic-refresh43', self.secrets)
        self.config['Client Secret'] = 'mutated-original'
        self.assertNotIn('mutated-original', str(tokens.configs))

    def test_mock_id_selection_disabled_acquisition_and_environment_isolation(self):
        config = {'Type':'Mock', 'Token':'t', 'ID Token':'id-value42', 'Use ID Token':True}
        tokens = self.tokens(config)
        self.assertEqual(self.fetch(tokens), 'id-value42')
        self.assertEqual(self.fetch(tokens, 'id'), 'id-value42')
        self.assertEqual(self.calls, [])
        self.assertEqual(httpclient.redactor(['t'])('t'), httpclient.MASK)
        disabled = self.tokens(dict(self.config, **{'Acquire Automatically':False}))
        with self.assertRaisesRegex(ValueError, 'disabled'):
            self.fetch(disabled)
        self.assertEqual(disabled.cache, {})

    def test_untrusted_child_cannot_supply_url_credentials_or_unknown_authentication_id(self):
        tokens = self.tokens()
        for message in (dict(kind='auth', id='unknown', token='access'), dict(kind='auth', id='main', token='refresh'),
                        dict(kind='auth', id='main', token='access', url='https://evil.example'),
                        dict(kind='auth', id=[], token='access')):
            with self.subTest(message=message), self.assertRaises(ValueError):
                tokens(message, time.monotonic() + 10)
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(tokens.failure)

    def test_bad_response_types_duplicates_and_nonfinite_expiry_never_become_a_token(self):
        for patch in ({'token_type':[]}, {'access_token':False}, {'expires_in':True}, {'expires_in':'nan'},
                      {'expires_in':-1}, {'expires_in':{}}, {'access_token':'secret\r\nheader'},
                      {'access_token':'a' * (auth.MAX_TOKEN + 1)}):
            with self.subTest(patch=patch):
                self.response.update(patch)
                with self.assertRaises(ValueError):
                    self.fetch(self.tokens())
                self.response = dict(access_token='synthetic-access42', expires_in=100)

    def test_header_injection_duplicate_protocol_fields_and_unresolved_credentials_do_not_send(self):
        for patch in ({'Custom Request Headers':{'X-Test':'yes\r\nInjected: true'}},
                      {'Custom Request Headers':{'Transfer-Encoding':'chunked'}},
                      {'Custom Request Headers':{'Authorization':'another'}},
                      {'Custom Request Parameters':{'grant_type':'password'}},
                      {'Client Secret':'{{unresolved}}'}, {'Client ID':False}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.fetch(self.tokens(dict(self.config, **patch)))
        self.assertEqual(self.calls, [])

    def test_endpoints_reject_insecure_remote_userinfo_fragment_invalid_port_and_host_substitution(self):
        for url in ('http://issuer.example/token', 'https://user:pass@issuer.example/token', 'https://issuer.example/#token',
                    'https://issuer.example:0/token', 'https://{{issuer}}/token', 'https://issuer.example/\r\nHost: other'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                auth.endpoint(url)
        self.assertEqual(auth.endpoint('http://127.0.0.1:1234/token').hostname, '127.0.0.1')
        self.assertEqual(auth.endpoint('https://issuer.example/token?q=value').query, 'q=value')

    def test_approval_can_show_plain_endpoints_but_never_credentials_or_sensitive_query_urls(self):
        config = dict(self.config, **{'Auth URL':'https://issuer.example/authorize',
            'Device Auth URL':'https://issuer.example/device', 'Redirect URL':'http://localhost:54321/callback'})
        public = {'Security':{'Auth':{'main':config}}}
        values = httpclient.secret_values(public, public)
        self.assertIn(self.config['Client Secret'], values)
        self.assertNotIn(self.config['Token URL'], values)
        self.assertNotIn(config['Auth URL'], values)
        private = {'Security':{'Auth':{'main':{'Token URL':'https://issuer.example/token?credential=private',
                   'Client Secret':'https://credential.example/secret'}}}}
        values = httpclient.secret_values({}, private)
        self.assertEqual(set(values), {'https://issuer.example/token?credential=private', 'https://credential.example/secret'})


class TransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                data = self.rfile.read(int(self.headers['Content-Length']))
                cls.calls.append((self.path, data))
                if self.path == '/slow':
                    time.sleep(.3)
                body = {'/valid':b'{"access_token":"synthetic42"}', '/duplicate':b'{"access_token":"one","access_token":"two"}',
                        '/large':b' ' * (auth.MAX_BODY + 1), '/nonfinite':b'{"expires_in":NaN}',
                        '/error':b'{"error_description":"never-print-synthetic-secret"}'}.get(self.path, b'{}')
                self.send_response(302 if self.path == '/redirect' else 401 if self.path == '/error' else 200)
                self.send_header('Location', '/never-follow')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:%d' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def post(self, path, timeout=2):
        return auth.post(self.url + path, [('client_secret', 'synthetic-secret42')],
                         {'Content-Type':'application/x-www-form-urlencoded'}, time.monotonic() + timeout)

    def test_real_issuer_success_no_redirect_and_no_error_description(self):
        self.assertEqual(self.post('/valid'), {'access_token':'synthetic42'})
        for path in ('/redirect', '/error', '/duplicate', '/large', '/nonfinite'):
            with self.subTest(path=path), self.assertRaises(ValueError) as caught:
                self.post(path)
            self.assertNotIn('never-print-synthetic-secret', str(caught.exception))
        self.assertFalse(any(path == '/never-follow' for path, _ in self.calls))

    def test_absolute_deadline_and_connect_finishing_late_never_sends_credentials(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'deadline'):
            self.post('/slow', .05)
        self.assertLess(time.monotonic() - started, .25)
        connected, closed, sent = threading.Event(), threading.Event(), []
        class LateConnection:
            sock = mock.Mock()
            def __init__(self, *args, **kwargs):
                pass
            def connect(self):
                time.sleep(.1)
                connected.set()
            def request(self, *args, **kwargs):
                sent.append(True)
                raise AssertionError('credential sent after deadline')
            def close(self):
                if connected.is_set():
                    closed.set()
        with mock.patch.object(auth.http.client, 'HTTPConnection', LateConnection):
            with self.assertRaisesRegex(ValueError, 'deadline'):
                self.post('/late', .02)
            self.assertTrue(connected.wait(1))
            self.assertTrue(closed.wait(1))
        self.assertEqual(sent, [])
        self.assertFalse(any(path == '/late' for path, _ in self.calls))

    def test_cancel_during_connect_returns_promptly_and_never_transmits_credentials_late(self):
        cancel, connected, closed, sent = threading.Event(), threading.Event(), threading.Event(), []
        class LateConnection:
            sock = mock.Mock()
            def __init__(self, *args, **kwargs):
                pass
            def connect(self):
                time.sleep(.15)
                connected.set()
            def request(self, *args, **kwargs):
                sent.append(True)
            def close(self):
                if connected.is_set():
                    closed.set()
        timer = threading.Timer(.02, cancel.set)
        with mock.patch.object(auth.http.client, 'HTTPConnection', LateConnection):
            timer.start()
            self.addCleanup(timer.join)
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                auth.post(self.url + '/cancelled-connect', [('client_secret','synthetic-secret42')], {},
                          time.monotonic() + 3, cancel=cancel)
            self.assertLess(time.monotonic() - started, .12)
            self.assertTrue(connected.wait(1))
            self.assertTrue(closed.wait(1))
        self.assertEqual(sent, [])



class BrowserSignInTest(unittest.TestCase):
    def setUp(self):
        import socket
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.redirect = 'http://127.0.0.1:%d/callback' % port
        self.config = {'Type':'OAuth2', 'Grant Type':'Authorization Code', 'Client ID':'fixture', 'Client Credentials':'none',
                       'Auth URL':'https://issuer.example/authorize', 'Token URL':'https://issuer.example/token',
                       'Redirect URL':self.redirect, 'PKCE':True}
        self.calls = []
        self.tokens = auth.Tokens({'browser':self.config}, [], self.transport)

    def transport(self, url, parameters, headers, deadline, context, **kwargs):
        self.calls.append((url, parameters, headers, kwargs))
        return {'access_token':'browser-access-token42', 'expires_in':3600}

    def flow(self, config=None, deadline=None):
        flow = auth.SignIn(self.tokens, config or self.tokens.configs['browser'], [('client_id','fixture')],
                           {'Content-Type':'application/x-www-form-urlencoded'}, deadline or time.monotonic() + 5)
        self.addCleanup(flow.close)
        return flow

    def callback(self, query):
        import http.client
        target = urllib.parse.urlsplit(self.redirect)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=3)
        try:
            connection.request('GET', target.path + '?' + urllib.parse.urlencode(query))
            response = connection.getresponse()
            data = response.read()
            self.assertIn("default-src 'none'", response.getheader('Content-Security-Policy'))
            return response.status, data
        finally:
            connection.close()

    def test_code_requires_open_and_exact_state_pkce_and_fixed_redirect_before_token_exchange(self):
        flow = self.flow()
        self.assertEqual(self.callback({'state':flow.state,'code':'not-open'})[0], 400)
        url = flow.open()
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(params['redirect_uri'], self.redirect)
        self.assertEqual(params['code_challenge_method'], 'S256')
        self.assertNotIn('code_verifier', params)
        self.assertNotIn(flow.state, json.dumps(flow.public))
        self.assertEqual(self.callback({'state':'wrong-state','code':'wrong-code'})[0], 400)
        self.assertEqual(self.callback([('state',flow.state),('state',flow.state),('code','duplicate')])[0], 400)
        self.assertFalse(flow.finished.is_set())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.callback({'state':flow.state,'code':'fixture-code42'})[0], 200)
        value = flow.wait(threading.Event())
        self.assertEqual(value['access_token'], 'browser-access-token42')
        sent = dict(self.calls[0][1])
        expected = base64.urlsafe_b64encode(__import__('hashlib').sha256(sent['code_verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(params['code_challenge'], expected)
        self.assertEqual(sent['code'], 'fixture-code42')
        self.assertEqual(sent['redirect_uri'], self.redirect)
        self.assertNotIn('code', flow.public)

    def test_callback_error_never_exposes_provider_description_or_exchanges_a_token(self):
        flow = self.flow()
        flow.open()
        status, data = self.callback({'state':flow.state,'error':'access_denied','error_description':'synthetic-password42'})
        self.assertEqual(status, 200)
        self.assertNotIn(b'synthetic-password42', data)
        with self.assertRaisesRegex(auth.OAuthError, 'access_denied') as caught:
            flow.wait(threading.Event())
        self.assertNotIn('synthetic-password42', str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_cancelled_completed_code_never_transmits_credentials_and_closes_callback_port(self):
        flow = self.flow()
        flow.open()
        self.callback({'state':flow.state,'code':'fixture-code42'})
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            flow.wait(cancel)
        self.assertIsNone(flow.server)
        self.assertEqual(self.calls, [])

    def test_external_or_ambiguous_callback_and_bad_pkce_fail_without_a_listener(self):
        for field, value in [('Redirect URL','https://app.example/callback'), ('Redirect URL',self.redirect + '?fixed=1'),
                             ('PKCE', {'Code Verifier':'short'}), ('PKCE', {'Code Challenge Method':'wrong'}),
                             ('Custom Request Parameters', {'state':'attacker-state'}),
                             ('Custom Request Headers', {'X-Auth':'synthetic-password42'})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.flow(dict(self.config, **{field:value}))
        self.assertEqual(self.calls, [])

    def test_device_pending_slow_down_and_success_keep_device_code_host_only(self):
        config = dict(self.config, **{'Grant Type':'Device Authorization','Device Auth URL':'https://issuer.example/device','Open Complete URI':True})
        poll = []
        def device_transport(url, parameters, headers, deadline, context, **kwargs):
            if url.endswith('/device'):
                return {'device_code':'secret-device-code42','user_code':'DEMO-CODE','verification_uri':'https://issuer.example/verify',
                        'verification_uri_complete':'https://issuer.example/verify?code=DEMO-CODE','expires_in':30,'interval':.001}
            poll.append(dict(parameters))
            if len(poll) < 3:
                raise auth.OAuthError('authorization_pending' if len(poll) == 1 else 'slow_down')
            return {'access_token':'device-access-token42'}
        self.tokens.transport = device_transport
        flow = self.flow(config, time.monotonic() + 15)
        self.assertNotIn('secret-device-code42', json.dumps(flow.public))
        self.assertEqual(flow.public['user_code'], 'DEMO-CODE')
        self.assertIn('code=DEMO-CODE', flow.open())
        result = flow.wait(threading.Event())
        self.assertEqual(result['access_token'], 'device-access-token42')
        self.assertEqual(len(poll), 3)
        self.assertEqual(poll[-1]['grant_type'], 'urn:ietf:params:oauth:grant-type:device_code')
        self.assertEqual(flow.interval, 6)

    def test_device_after_browser_sends_no_poll_until_its_gui_popup_is_closed(self):
        calls = []
        def device_transport(url, parameters, headers, deadline, context, **kwargs):
            if url.endswith('/device'):
                return {'device_code':'private-device42','user_code':'CODE42','verification_uri':'https://issuer.example/verify',
                        'expires_in':30,'interval':1}
            calls.append(time.monotonic())
            return {'access_token':'completed-device42'}
        self.tokens.transport = device_transport
        config = dict(self.config, **{'Grant Type':'Device Authorization','Device Auth URL':'https://issuer.example/device',
                                     'Start Polling After Browser':True})
        flow = self.flow(config)
        flow.open()
        values = []
        cancel = threading.Event()
        worker = threading.Thread(target=lambda:values.append(flow.wait(cancel)))
        worker.start()
        try:
            time.sleep(.1)
            self.assertEqual(calls, [])
            flow.browser_closed.set()
            worker.join(2)
            self.assertEqual(values, [{'access_token':'completed-device42'}])
        finally:
            cancel.set()
            worker.join(2)
        self.assertEqual(len(calls), 1)

    def test_tokens_use_completed_signin_once_then_the_existing_cache(self):
        def signin(name, flow, deadline):
            self.assertEqual(name, 'browser')
            flow.open()
            self.callback({'state':flow.state,'code':'fixture-code42'})
            return flow.wait(threading.Event())
        self.tokens.signin = mock.Mock(side_effect=signin)
        message = dict(kind='auth', id='browser', token='access')
        first = self.tokens(message, time.monotonic() + 5)
        second = self.tokens(message, time.monotonic() + 5)
        self.assertEqual(first, second)
        self.assertEqual(first['token'], 'browser-access-token42')
        self.tokens.signin.assert_called_once()
        self.assertEqual(len(self.calls), 1)
