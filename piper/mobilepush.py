"""Optional peer-bound Web Push; session names require per-phone opt-in, never messages/bearers.

Only mobile bridge imports the optional crypto/network libraries. piperd uses the
stdlib-only snapshot() to expose a cheap state view, without claiming GUI presence.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import secrets
import time
from time import monotonic
from urllib.parse import urlsplit

import mobile
import common

PROVIDERS = {'web.push.apple.com', 'fcm.googleapis.com', 'updates.push.services.mozilla.com'}
POLL_SECONDS = 3


def clean_title(value) -> str:
    if not isinstance(value, str):
        return ''
    visible = common.no_bidi(value).translate(dict.fromkeys(common.INVISIBLE))
    visible = ''.join(c if ord(c) >= 32 and ord(c) != 127 else ' ' for c in visible)
    return common.scrub(' '.join(visible.split()))[:100]


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def unb64(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > 128 or not value or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in value):
        raise ValueError('invalid push key')
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


def subscription(value) -> dict:
    """Push endpoints are capabilities, never arbitrary HTTP proxy destinations."""
    from cryptography.hazmat.primitives.asymmetric import ec
    if not isinstance(value, dict) or not isinstance(value.get('endpoint'), str):
        raise ValueError('invalid push subscription')
    endpoint = value['endpoint']
    url = urlsplit(endpoint)
    if (len(endpoint) > 4096 or url.scheme != 'https' or url.hostname not in PROVIDERS
            or url.username or url.password or url.port not in (None, 443) or url.fragment
            or not url.path or url.path == '/' or '\\' in endpoint
            or any(ord(c) <= 32 or ord(c) > 126 for c in endpoint)):
        raise ValueError('unsupported push endpoint')
    keys = value.get('keys')
    if not isinstance(keys, dict):
        raise ValueError('invalid push keys')
    public, auth = unb64(keys.get('p256dh')), unb64(keys.get('auth'))
    if len(public) != 65 or public[0] != 4 or len(auth) != 16:
        raise ValueError('invalid push keys')
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    canonical = 'https://' + url.hostname + url.path + ('?' + url.query if url.query else '')
    return {'endpoint': canonical, 'keys': {'p256dh': b64(public), 'auth': b64(auth)}}


def _device(state, peer, expected):
    device = state['devices'].get(peer)
    if not expected or not device or device.get('grant') != expected:
        raise PermissionError('device authorization ended')
    return device


def _vapid(state):
    from py_vapid import Vapid
    if 'push_key' not in state:
        key = Vapid(); key.generate_keys()
        state['push_key'] = key.private_pem().decode()
    return Vapid.from_pem(state['push_key'].encode())


def action(peer: str, expected: str, value: dict) -> dict:
    """The authenticated QUIC peer may administer only its own current subscription."""
    available = importlib.util.find_spec('pywebpush') is not None
    command = value.get('action')
    if command not in ('status', 'subscribe', 'unsubscribe', 'test', 'preferences'):
        raise ValueError('unknown push action')
    if command == 'preferences' and not isinstance(value.get('showTitles'), bool):
        raise ValueError('notification preference must be boolean')
    prepared = subscription(value.get('subscription')) if command == 'subscribe' and available else None
    with mobile.state_lock():
        state = mobile.load_state(); device = _device(state, peer, expected)
        if command == 'unsubscribe':
            if device.get('push', {}).get('id') == value.get('subscriptionId'):
                device.pop('push', None)
        elif command == 'preferences':
            show = value['showTitles']
            if device.get('push_titles', False) != show:
                device['push_titles'] = show
                if device.get('push'):
                    device['push']['id'] = secrets.token_hex(16)  # queued/encrypted old preferences cannot dispatch
        elif not available:
            return {'available': False, 'enabled': False, 'publicKey': '', 'showTitles': bool(device.get('push_titles'))}
        elif command == 'subscribe':
            for other, record in state['devices'].items():
                if other != peer and record.get('push', {}).get('subscription', {}).get('endpoint') == prepared['endpoint']:
                    raise ValueError('push subscription belongs to another device')
            if device.get('push', {}).get('subscription') != prepared:
                device['push'] = {'id': secrets.token_hex(16), 'subscription': prepared}
        elif command == 'test':
            record = device.get('push')
            if not record or time.time() - record.get('test_at', 0) < 10:
                raise ValueError('enable notifications or wait before another test')
            record['test_at'] = time.time()
        key = _vapid(state) if available else None
        mobile.save_state(state)
        record = device.get('push')
    if command == 'test':
        return {'sent': send(peer, expected, record['id'], kind='test') in (200, 201, 202)}
    public = ''
    if key:
        from cryptography.hazmat.primitives import serialization
        public = b64(key.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    return {'available': available, 'enabled': bool(record), 'publicKey': public,
            'showTitles': bool(device.get('push_titles')), 'subscriptionId': record['id'] if record else ''}


def _current(peer, expected, identity):
    state = mobile.load_state()
    device = _device(state, peer, expected)
    record = device.get('push')
    if not record or record.get('id') != identity:
        raise PermissionError('push subscription changed')
    return state, record


def send(peer: str, expected: str, identity: str, *, kind='attention', session_title='') -> int:
    """Encrypt a generic alert or explicitly opted-in name; recheck grant/preferences at dispatch.

    Success means accepted by the provider, never confirmed display on the phone.
    Zero means revoked/replaced/unavailable; no endpoint or exception text is logged.
    """
    import requests
    from pywebpush import webpush, WebPushException
    from py_vapid import VapidException

    class PushSession(requests.Session):
        def post(self, url, **kwargs):
            _current(peer, expected, identity)  # encryption/executor may have waited before dispatch
            kwargs.update(allow_redirects=False, timeout=(3, 7))
            return super().post(url, **kwargs)

        def send(self, request, **kwargs):
            # Request preparation can also wait. This is the start of HTTP dispatch;
            # once entered, the send is in flight and cannot be recalled by revoke.
            _current(peer, expected, identity)
            return super().send(request, **kwargs)

    try:
        state, record = _current(peer, expected, identity)
        clean = subscription(record['subscription'])
        key = _vapid(state)
        contact = mobile.https_url(state.get('client', 'https://piper.kamest.dev'))
        # We validate the HTTPS contact origin above. py-vapid's optional regex
        # rejects valid origins with a port/IPv6; this does not relax JWT signing.
        key.conf['no-strict'] = True
        payload = {'v': 1, 'kind': kind}
        title = clean_title(session_title)
        if kind == 'attention' and state['devices'][peer].get('push_titles') and title:
            payload['session'] = title
        with PushSession() as session:
            session.trust_env = False  # no proxy credentials/netrc from the host environment
            response = webpush(clean, json.dumps(payload), vapid_private_key=key,
                vapid_claims={'sub': contact},
                ttl=300, timeout=7, headers={'Urgency': 'normal', 'Topic': 'piper-attention'}, requests_session=session)
            status = response.status_code
    except PermissionError:
        return 0
    except WebPushException as error:
        status = error.response.status_code if error.response is not None else 503
    except (OSError, ValueError, KeyError, VapidException, requests.RequestException):
        return 503
    if status in (404, 410):
        with mobile.state_lock():
            state = mobile.load_state()
            with contextlib.suppress(PermissionError):
                device = _device(state, peer, expected)
                if device.get('push', {}).get('id') == identity:
                    device.pop('push'); mobile.save_state(state)
    return status


def snapshot(manager) -> dict:
    """Cheap local state, under the existing manager lock; never read transcripts/files/GUI presence."""
    with manager.lock:
        visible = {s.id: s for s in manager.sessions.values() if not s.hidden and not s.id.startswith('provider-')}
        return {'sessions': [{'id': s.id, 'state': s.state, 'since': s.since, 'title': clean_title(getattr(s, 'title', ''))}
                             for s in visible.values() if s.kind == 'project'],
                'pending': [p['id'] for p in manager.pending.values() if p.get('session') in visible],
                'pending_sessions': {p['id']: p['session'] for p in manager.pending.values() if p.get('session') in visible}}


class Events:
    """Suppress initial backlog; detect new attention states/cards without retaining user text."""
    def __init__(self):
        self.previous = None
        self.session_title = ''

    def changed(self, value):
        states = {s['id']: (s['state'], s['since']) for s in value['sessions']}
        cards = set(value['pending'])
        before = self.previous; self.previous = states, cards
        self.session_title = ''
        if before is None:
            return False
        changed = [sid for sid, state in states.items() if state[0] in ('waiting', 'attention', 'trust') and before[0].get(sid) != state]
        new_cards = cards - before[1]
        owners = [value.get('pending_sessions', {}).get(card) for card in sorted(new_cards)]
        candidate = next((sid for sid in owners if sid), changed[-1] if changed else None)
        self.session_title = next((clean_title(s.get('title')) for s in value['sessions'] if s['id'] == candidate), '')
        return bool(new_cards or changed)


def _events(token):
    meta, data = mobile.proxy_http({'path': '/api/mobile/events'}, b'', token, lambda: True, timeout=3)
    if meta['status'] != 200:
        raise ValueError('notification events unavailable')
    return json.loads(data)


async def run(token):
    """Bounded/coalesced delivery, independent of GUI and live terminal connections."""
    events = Events()
    waiting, last_sent = {}, {}
    semaphore = asyncio.Semaphore(4)

    async def deliver(peer, expected, identity, title):
        async with semaphore:
            return await asyncio.to_thread(send, peer, expected, identity, session_title=title)

    while True:
        try:
            state = mobile.load_state()
            devices = {peer: (d['grant'], d['push']['id']) for peer, d in state['devices'].items() if d.get('push')}
            now = monotonic()
            if not devices or importlib.util.find_spec('pywebpush') is None:
                events = Events(); waiting.clear(); last_sent.clear()
            else:
                if events.changed(await asyncio.to_thread(_events, token)):
                    for peer, identity in devices.items():
                        if waiting.get(peer, {}).get('identity') != identity:
                            waiting[peer] = {'identity': identity, 'expires': now + 300, 'due': max(now, last_sent.get(peer, 0) + 10), 'attempt': 0}
                        waiting[peer]['title'] = events.session_title
                for peer, item in list(waiting.items()):
                    if devices.get(peer) != item['identity'] or item['expires'] <= now:
                        waiting.pop(peer, None)
                due = [(peer, item) for peer, item in waiting.items() if item['due'] <= now]
                results = await asyncio.gather(*(deliver(peer, *item['identity'], item['title']) for peer, item in due))
                for (peer, item), status in zip(due, results):
                    if status in (0, 200, 201, 202, 404, 410):
                        waiting.pop(peer, None)
                        if status in (200, 201, 202): last_sent[peer] = monotonic()
                    else:
                        item['attempt'] += 1
                        item['due'] = monotonic() + min(30 * 2 ** min(item['attempt'] - 1, 4), 300)
                last_sent = {peer: at for peer, at in last_sent.items() if peer in devices}
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, TypeError, KeyError, PermissionError):
            pass  # fail closed; the next bounded poll retries, without leaking endpoint/token details
        await asyncio.sleep(POLL_SECONDS)
