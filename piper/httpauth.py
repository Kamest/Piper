"""Frozen IDEA authentication configurations and one-run managed tokens.

The child supplies only a configuration ID and token kind. URLs, credentials and
TLS policy are fixed by the host before approval; redirects never carry secrets.
No provider credential store or persistent token cache is consulted.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import http.client
import http.server
import json
import math
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.parse

MAX_BODY = 1 << 20
MAX_TOKEN = 24 << 10
HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
OAUTH_ERRORS = frozenset(('invalid_request', 'invalid_client', 'invalid_grant', 'unauthorized_client', 'unsupported_grant_type',
                          'invalid_scope', 'access_denied', 'authorization_pending', 'slow_down', 'expired_token',
                          'server_error', 'temporarily_unavailable', 'unsupported_response_type'))


class OAuthError(ValueError):
    """A fixed RFC error code, never an issuer's free-text description."""
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in OAUTH_ERRORS else 'invalid_request'
        super().__init__('authentication failed (' + self.code + ')')


def configurations(public, private, env, values, substitute):
    """Merge each ID independently, resolving the approved environment snapshot."""
    result = {}
    for document in (public, private):
        section = (document.get(env) or {}).get('Security', {})
        auth = section.get('Auth', {}) if isinstance(section, dict) else {}
        if not isinstance(auth, dict):
            raise ValueError('Security.Auth must be an object')
        for name, config in auth.items():
            if not isinstance(name, str) or not name or len(name) > 1024 or not isinstance(config, dict):
                raise ValueError('invalid Security.Auth configuration')
            result.setdefault(name, {}).update(copy.deepcopy(config))
    def resolve(value):
        if isinstance(value, str):
            return substitute(value, values)
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, dict):
            return {key:resolve(item) for key, item in value.items()}
        return value
    return resolve(result)


def endpoint(value):
    """Credential-bearing requests require TLS, except explicit loopback development."""
    if not isinstance(value, str) or len(value) > 8192 or '{{' in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError('invalid managed authentication endpoint')
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = parsed.hostname and parsed.port != 0 and parsed.username is None and parsed.password is None and not parsed.fragment
    except ValueError:
        valid = False
    if not valid or parsed.scheme not in ('https', 'http') or parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('managed authentication requires an HTTPS endpoint (HTTP is allowed only on loopback)')
    return parsed


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('ambiguous authentication response')
        out[key] = value
    return out


def post(url, parameters, headers, deadline, context=None, *, cancel=None, oauth_errors=False):
    """One bounded POST; no redirects, proxy environment, retry or response-body log.

    DNS may outlive the deadline in a daemon worker. It checks cancellation after
    connect, before transmitting any credential bytes; the caller never waits for it.
    """
    target = endpoint(url)
    data = urllib.parse.urlencode(parameters, doseq=True).encode('utf-8')
    if len(data) > MAX_BODY:
        raise ValueError('authentication request exceeds 1 MiB')
    stopped, done = threading.Event(), threading.Event()
    connections, result = [], []
    def remaining():
        seconds = deadline - time.monotonic()
        if stopped.is_set() or cancel is not None and cancel.is_set() or seconds <= 0:
            raise ValueError('authentication deadline exceeded')
        return seconds
    def worker():
        connection = None
        try:
            options = dict(timeout=remaining())
            if target.scheme == 'https':
                options['context'] = context or ssl.create_default_context()
            cls = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
            connection = cls(target.hostname, target.port, **options)
            connections.append(connection)
            connection.connect()
            connection.sock.settimeout(remaining())
            path = urllib.parse.urlunsplit(('', '', target.path or '/', target.query, ''))
            connection.request('POST', path, data, headers)
            response = connection.getresponse()
            if response.status != 200 and not (oauth_errors and response.status == 400):
                raise ValueError('authentication endpoint returned HTTP %d; not retried' % response.status)
            chunks, size = [], 0
            while True:
                # read1 returns after one underlying read, so a slow peer cannot
                # extend the absolute run deadline by dribbling a whole read(n).
                if connection.sock:
                    connection.sock.settimeout(remaining())
                else:
                    remaining()
                chunk = response.read1(min(65536, MAX_BODY + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BODY:
                    raise ValueError('authentication response exceeds 1 MiB')
            remaining()
            value = json.loads(b''.join(chunks), object_pairs_hook=_unique,
                               parse_constant=lambda _:(_ for _ in ()).throw(ValueError('non-finite authentication value')))
            if not isinstance(value, dict):
                raise ValueError('invalid authentication response')
            if response.status != 200:
                raise OAuthError(value.get('error'))
            result.append(value)
        except Exception as error:
            # Neither server error descriptions nor socket exceptions are safe to
            # expose (they can echo passwords, URLs or token response content).
            result.append(ValueError('authentication deadline exceeded; not retried') if stopped.is_set() or time.monotonic() >= deadline else
                          error if isinstance(error, OAuthError) or type(error) is ValueError and str(error).startswith(('authentication ', 'invalid authentication', 'ambiguous authentication', 'non-finite authentication'))
                          else ValueError('authentication transport or response failed; not retried'))
        finally:
            if connection:
                connection.close()
            done.set()
    worker_thread = threading.Thread(target=worker, name='piper-http-auth', daemon=True)
    worker_thread.start()
    while not done.wait(min(.05, max(0, deadline - time.monotonic()))):
        if time.monotonic() < deadline and not (cancel is not None and cancel.is_set()):
            continue
        stopped.set()
        for connection in connections:
            if connection.sock:
                try:
                    connection.sock.shutdown(2)
                except OSError:
                    pass
                connection.close()
        raise ValueError('authentication cancelled; not retried' if cancel is not None and cancel.is_set() else 'authentication deadline exceeded; not retried')
    if isinstance(result[0], Exception):
        raise result[0]
    return result[0]


class Tokens:
    """Lazy token acquisition restricted to the immutable approved configuration."""
    def __init__(self, configs, secrets, transport=post):
        self.configs, self.secrets, self.transport = copy.deepcopy(configs), secrets, transport
        self.cache, self.failure = {}, None
        self.context = None
        self.context_for = lambda url:self.context
        self.signin = None
        self.route = lambda url:url
        self.digest = hashlib.sha256(json.dumps(self.configs, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        self.endpoints = []
        for config in self.configs.values():
            if config.get('Type') == 'OAuth2':
                for field in ('Token URL', 'Auth URL', 'Device Auth URL'):
                    url = config.get(field)
                    if url is not None:
                        endpoint(url)
                        if url not in self.endpoints:
                            self.endpoints.append(url)
            for name in ('Client Secret', 'Password', 'Token', 'ID Token'):
                if isinstance(config.get(name), str) and config[name]:
                    self.secrets.append(config[name])

    def _extras(self, config, phase='token'):
        parameters, headers = [], {'Content-Type':'application/x-www-form-urlencoded', 'Accept':'application/json'}
        for field, target in (('Custom Request Parameters', parameters), ('Custom Request Headers', headers)):
            values = config.get(field, {})
            if not isinstance(values, dict):
                raise ValueError('invalid managed authentication customization')
            for name, value in values.items():
                if not isinstance(name, str) or not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
                    raise ValueError('invalid managed authentication customization')
                if isinstance(value, dict):
                    if set(value) - {'Value', 'Use'} or value.get('Use', 'Everywhere') not in ('Everywhere', 'In Auth Request', 'In Token Request'):
                        raise ValueError('invalid managed authentication customization')
                    if value.get('Use') == ('In Auth Request' if phase == 'token' else 'In Token Request'):
                        continue
                    value = value.get('Value')
                if isinstance(target, dict):
                    if (not HEADER.fullmatch(name) or name.lower() in ('host', 'connection', 'content-length', 'transfer-encoding', 'trailer', 'upgrade', 'proxy-authorization', 'proxy-connection') or
                            not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                        raise ValueError('invalid managed authentication header')
                    # Header names are case-insensitive; never emit duplicate variants.
                    for old in list(target):
                        if old.lower() == name.lower():
                            del target[old]
                    target[name] = value
                else:
                    values = value if isinstance(value, list) else [value]
                    if any(not isinstance(item, str) or '{{' in item for item in values):
                        raise ValueError('unresolved managed authentication parameter')
                    target.extend((name, item) for item in values)
        return parameters, headers

    def _token(self, config, kind, deadline):
        if config.get('Type') == 'Mock':
            value = config.get('ID Token' if kind == 'id' or config.get('Use ID Token') is True else 'Token')
            return self._secret(value)
        if config.get('Type') != 'OAuth2':
            raise ValueError('unknown managed authentication type')
        cached = self.cache.get(id(config))
        if cached and time.monotonic() < cached['expires']:
            return self._secret(cached.get('id_token' if kind == 'id' or config.get('Use ID Token') is True else 'access_token'))
        if config.get('Acquire Automatically', True) is not True:
            raise ValueError('managed authentication automatic acquisition is disabled; acquire a token first')
        parameters, headers = self._extras(config)
        interactive = False
        if cached and cached.get('refresh_token'):
            parameters += [('grant_type', 'refresh_token'), ('refresh_token', cached['refresh_token'])]
        elif config.get('Grant Type') == 'Client Credentials':
            parameters.append(('grant_type', 'client_credentials'))
        elif config.get('Grant Type') == 'Password':
            parameters += [('grant_type', 'password'), ('username', self._required(config, 'Username')), ('password', self._required(config, 'Password'))]
        elif config.get('Grant Type') in ('Authorization Code', 'Device Authorization') and self.signin is not None:
            interactive = True
        else:
            raise ValueError('managed interactive authentication is not yet available; no protected request was sent')
        client = self._required(config, 'Client ID')
        secret = config.get('Client Secret', '')
        if not isinstance(secret, str) or '{{' in secret:
            raise ValueError('unresolved managed authentication credential')
        method = config.get('Client Credentials', 'basic')
        if method == 'basic':
            if any(name.lower() == 'authorization' for name in headers):
                raise ValueError('ambiguous managed authentication authorization header')
            raw = urllib.parse.quote_plus(client, safe='') + ':' + urllib.parse.quote_plus(secret, safe='')
            headers['Authorization'] = 'Basic ' + base64.b64encode(raw.encode()).decode()
        elif method == 'in body':
            parameters += [('client_id', client), ('client_secret', secret)]
        elif method == 'none':
            parameters.append(('client_id', client))
        else:
            raise ValueError('unknown managed authentication client credential method')
        if config.get('Scope'):
            parameters.append(('scope', self._required(config, 'Scope')))
        # Standard protocol fields cannot have ambiguous attacker-defined duplicates.
        names = [name for name, _ in parameters]
        for name in ('grant_type', 'client_id', 'client_secret', 'username', 'password', 'refresh_token', 'scope'):
            if names.count(name) > 1:
                raise ValueError('ambiguous managed authentication parameter')
        if interactive:
            if any(name in ('code', 'redirect_uri', 'code_verifier', 'device_code', 'grant_type') for name, _ in parameters):
                raise ValueError('ambiguous interactive authentication parameter')
            flow = SignIn(self, config, parameters, headers, deadline)
            try:
                name = next(key for key, item in self.configs.items() if item is config)
                value = self.signin(name, flow, deadline)
            finally:
                flow.close()
        else:
            url = self.route(config.get('Token URL'))
            value = self.transport(url, parameters, headers, deadline, self.context_for(url))
        for name in ('access_token', 'id_token', 'refresh_token'):
            if name in value:
                self._secret(value[name])
        token_type = value.get('token_type', 'Bearer')
        if not value.get('access_token') or not isinstance(token_type, str) or not token_type or any(ord(c) <= 32 for c in token_type):
            raise ValueError('invalid authentication token response')
        expires = value.get('expires_in')
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float, str))):
            raise ValueError('invalid authentication token expiry')
        try:
            lifetime = float(expires) if expires is not None else math.inf
        except (ValueError, OverflowError):
            raise ValueError('invalid authentication token expiry') from None
        if expires is not None and (not math.isfinite(lifetime) or lifetime < 0):
            raise ValueError('invalid authentication token expiry')
        value['expires'] = time.monotonic() + lifetime
        if cached and 'refresh_token' not in value and cached.get('refresh_token'):
            value['refresh_token'] = cached['refresh_token']
        self.cache[id(config)] = value
        return self._secret(value.get('id_token' if kind == 'id' or config.get('Use ID Token') is True else 'access_token'))

    @staticmethod
    def _required(config, name):
        value = config.get(name)
        if not isinstance(value, str) or not value or '{{' in value:
            raise ValueError('missing or unresolved managed authentication field: ' + name)
        return value

    def _secret(self, value):
        if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > MAX_TOKEN or
                any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            raise ValueError('invalid authentication token value')
        if value not in self.secrets:
            self.secrets.append(value)
        return value

    def __call__(self, message, deadline):
        try:
            if (set(message) != {'kind', 'id', 'token'} or message.get('kind') != 'auth' or
                    not isinstance(message.get('id'), str) or message.get('token') not in ('access', 'id') or
                    message['id'] not in self.configs or time.monotonic() >= deadline):
                raise ValueError('invalid managed authentication capability')
            return dict(token=self._token(self.configs[message['id']], message['token'], deadline))
        except (ValueError, OSError):
            self.failure = 'managed authentication failed; protected request was not sent; not retried'
            raise


class SignIn:
    """One run's browser authorization; protocol secrets remain on the host stack.

    The GUI can open the prebuilt URI or cancel. It cannot inject callback codes,
    choose a token endpoint, approve an unfinished flow or extend its deadline.
    """
    def __init__(self, tokens, config, parameters, headers, deadline):
        self.tokens, self.config = tokens, config
        self.parameters, self.headers = list(parameters), dict(headers)
        self.deadline, self.server = deadline, None
        self.opened, self.finished, self.closed = threading.Event(), threading.Event(), threading.Event()
        self.browser_closed = threading.Event()
        self.result, self.lock = None, threading.Lock()
        self.grant = config.get('Grant Type')
        self.public = dict(grant=self.grant, state='ready')
        if self.grant == 'Authorization Code':
            self._code()
        elif self.grant == 'Device Authorization':
            self._device()
        else:
            raise ValueError('unsupported interactive authentication grant')

    def _post(self, url, parameters, headers, cancel, errors=False):
        url = self.tokens.route(url)
        return self.tokens.transport(url, parameters, headers, self.deadline, self.tokens.context_for(url),
                                     cancel=cancel, oauth_errors=errors)

    def _code(self):
        config = self.config
        auth_url = endpoint(self.tokens._required(config, 'Auth URL'))
        redirect = self.tokens._required(config, 'Redirect URL')
        target = endpoint(redirect)
        if (target.scheme != 'http' or target.hostname not in ('127.0.0.1', 'localhost', '::1') or
                not target.port or target.query or not target.path.startswith('/')):
            raise ValueError('browser authentication needs its configured HTTP loopback Redirect URL; no protected request was sent')
        extras, headers = self.tokens._extras(config, phase='auth')
        if set(headers) != {'Content-Type', 'Accept'} or headers['Accept'] != 'application/json' or headers['Content-Type'] != 'application/x-www-form-urlencoded':
            raise ValueError('custom authorization headers require an embedded browser; no protected request was sent')
        self.state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        self.tokens.secrets.extend([self.state, nonce])
        params = [('response_type', 'code'), ('client_id', self.tokens._required(config, 'Client ID')),
                  ('redirect_uri', redirect), ('state', self.state), ('nonce', nonce)]
        if config.get('Scope'):
            params.append(('scope', self.tokens._required(config, 'Scope')))
        self.parameters += [('grant_type', 'authorization_code'), ('redirect_uri', redirect)]
        pkce = config.get('PKCE', False)
        if pkce is not False:
            if pkce is not True and not isinstance(pkce, dict):
                raise ValueError('invalid PKCE configuration')
            options = {} if pkce is True else pkce
            if set(options) - {'Code Challenge Method', 'Code Verifier'}:
                raise ValueError('invalid PKCE configuration')
            verifier = options.get('Code Verifier', secrets.token_urlsafe(48))
            if not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
                raise ValueError('invalid PKCE verifier')
            self.tokens.secrets.append(verifier)
            method = options.get('Code Challenge Method', 'SHA-256')
            if method not in ('Plain', 'SHA-256'):
                raise ValueError('invalid PKCE challenge method')
            challenge = verifier if method == 'Plain' else base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            params += [('code_challenge', challenge), ('code_challenge_method', 'plain' if method == 'Plain' else 'S256')]
            self.parameters.append(('code_verifier', verifier))
            self.public['pkce'] = True
        existing = urllib.parse.parse_qsl(auth_url.query, keep_blank_values=True, strict_parsing=True)
        reserved = {'response_type', 'client_id', 'redirect_uri', 'state', 'nonce', 'scope', 'code_challenge', 'code_challenge_method',
                    'client_secret', 'code', 'grant_type', 'code_verifier'}
        if any(name in reserved for name, _ in existing + extras):
            raise ValueError('ambiguous authorization parameter')
        self.url = urllib.parse.urlunsplit((auth_url.scheme, auth_url.netloc, auth_url.path,
                                           urllib.parse.urlencode(existing + params + extras), ''))
        self.public['provider'] = urllib.parse.urlunsplit((auth_url.scheme, auth_url.netloc, '', '', ''))
        owner = self
        class Callback(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                accepted = False
                try:
                    parsed = urllib.parse.urlsplit(self.path)
                    fields = _unique(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16))
                    state = fields.get('state')
                    valid = (len(self.path) <= 8192 and parsed.path == target.path and not parsed.scheme and not parsed.netloc and
                             owner.opened.is_set() and not owner.closed.is_set() and time.monotonic() < owner.deadline and
                             isinstance(state, str) and state.isascii() and hmac.compare_digest(state, owner.state))
                    if valid:
                        code, error = fields.get('code'), fields.get('error')
                        if isinstance(code, str) and 0 < len(code) <= MAX_TOKEN and not error and all(ord(c) > 32 and ord(c) != 127 for c in code):
                            value = code
                        elif error and not code:
                            value = OAuthError(error)
                        else:
                            raise ValueError('invalid callback')
                        with owner.lock:
                            if not owner.finished.is_set():
                                owner.result = value
                                owner.finished.set()
                                accepted = True
                except (ValueError, TypeError):
                    pass
                body = b'You may close this window.' if accepted else b'Invalid or expired sign-in callback.'
                self.send_response(200 if accepted else 400)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

        class Server(http.server.HTTPServer):
            address_family = socket.AF_INET6 if target.hostname == '::1' else socket.AF_INET
            allow_reuse_address = False
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(min(2, max(.1, owner.deadline - time.monotonic())))
                return connection, address
            def handle_error(self, *args):
                pass   # callback input and socket exceptions never enter daemon logs
        try:
            self.server = Server(('::1' if target.hostname == '::1' else '127.0.0.1', target.port), Callback)
        except OSError:
            raise ValueError('configured sign-in callback port is unavailable; no protected request was sent') from None
        self.server.timeout = .1
        def serve():
            while not self.closed.is_set() and not self.finished.is_set() and time.monotonic() < self.deadline:
                self.server.handle_request()
        self.thread = threading.Thread(target=serve, name='piper-http-auth-callback', daemon=True)
        self.thread.start()

    def _device(self):
        extras, headers = self.tokens._extras(self.config, phase='auth')
        reserved = {'client_id', 'scope', 'client_secret', 'grant_type', 'device_code'}
        if any(name in reserved for name, _ in extras):
            raise ValueError('ambiguous device authorization parameter')
        params = [('client_id', self.tokens._required(self.config, 'Client ID'))] + extras
        if self.config.get('Scope'):
            params.append(('scope', self.tokens._required(self.config, 'Scope')))
        value = self._post(self.config.get('Device Auth URL'), params, headers, self.closed, True)
        self.device_code = self.tokens._secret(value.get('device_code'))
        code = value.get('user_code')
        if not isinstance(code, str) or not code or len(code) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in code):
            raise ValueError('invalid authentication device user code')
        uri = value.get('verification_uri')
        endpoint(uri)
        self.url = value.get('verification_uri_complete', uri) if self.config.get('Open Complete URI') is True else uri
        opened_uri, shown_uri = endpoint(self.url), endpoint(uri)
        if (opened_uri.scheme, opened_uri.hostname, opened_uri.port) != (shown_uri.scheme, shown_uri.hostname, shown_uri.port):
            raise ValueError('device verification URI origin does not match the displayed provider')
        self.public.update(provider=uri, user_code=code)
        expiry, interval = value.get('expires_in'), value.get('interval', 5)
        if (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0 or
                type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0):
            raise ValueError('invalid authentication device expiry or interval')
        self.deadline = min(self.deadline, time.monotonic() + expiry)
        self.interval = max(1, interval)
        after_browser = self.config.get('Start Polling After Browser', False)
        if type(after_browser) is not bool:
            raise ValueError('invalid device browser polling option')
        self.public['after_browser'] = after_browser
        self.parameters += [('grant_type', 'urn:ietf:params:oauth:grant-type:device_code'), ('device_code', self.device_code)]

    def open(self):
        if self.closed.is_set() or time.monotonic() >= self.deadline:
            raise ValueError('sign-in deadline exceeded')
        self.opened.set()
        return self.url

    def wait(self, cancel):
        def active():
            if cancel.is_set() or self.closed.is_set():
                raise ValueError('authentication cancelled; no protected request was sent')
            if time.monotonic() >= self.deadline:
                raise ValueError('authentication deadline exceeded; no protected request was sent')
        try:
            if self.grant == 'Authorization Code':
                while not self.finished.wait(.05):
                    active()
                active()
                if isinstance(self.result, Exception):
                    raise self.result
                code = self.tokens._secret(self.result)
                active()
                return self._post(self.config.get('Token URL'), self.parameters + [('code', code)], self.headers, cancel, True)
            if self.config.get('Start Polling After Browser') is True:
                while not self.browser_closed.wait(.05):
                    active()
            while True:
                active()
                if cancel.wait(min(self.interval, max(0, self.deadline - time.monotonic()))):
                    active()
                active()
                try:
                    value = self._post(self.config.get('Token URL'), self.parameters, self.headers, cancel, True)
                    # Default polling may finish before this run's mandatory GUI
                    # sign-in action. Never send its protected HTTP request yet.
                    while not self.opened.wait(.05):
                        active()
                    active()
                    return value
                except OAuthError as error:
                    if error.code == 'slow_down':
                        self.interval += 5
                    elif error.code != 'authorization_pending':
                        raise
        finally:
            self.close()

    def close(self):
        self.closed.set()
        if self.server:
            self.thread.join(timeout=2.2)
            self.server.server_close()
            self.server = None
