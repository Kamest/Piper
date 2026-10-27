"""Peer-bound Web Push, privacy, real encryption and bounded delivery failures."""
import base64
import json
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

import _env  # noqa: F401
import mobile
import mobilepush

try:
    import pywebpush
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    pywebpush = None


@unittest.skipIf(pywebpush is None, 'optional Web Push runtime not installed')
class MobilePushTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(mobile, 'STATE', os.path.join(self.temp.name, 'mobile.json'))
        patch.start(); self.addCleanup(patch.stop)
        self.peer, self.grant = '11' * 32, 'test-grant'
        self.other = '22' * 32
        with mobile.state_lock():
            mobile.save_state({'v': 1, 'devices': {self.peer: {'grant': self.grant, 'label': 'Phone', 'at': 0}, self.other: {'grant': 'other-grant', 'label': 'Other', 'at': 0}}})
        self.key = ec.generate_private_key(ec.SECP256R1())
        raw = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.subscription = {'endpoint': 'https://web.push.apple.com/' + 'x' * 40,
            'keys': {'p256dh': mobilepush.b64(raw), 'auth': mobilepush.b64(b'fixture-auth-key')}}

    def api(self, action, **values):
        if action == 'unsubscribe' and 'subscriptionId' not in values:
            values['subscriptionId'] = mobile.load_state()['devices'][self.peer].get('push', {}).get('id', '')
        return mobilepush.action(self.peer, self.grant, {'action': action, **values})

    def enable(self):
        self.api('subscribe', subscription=self.subscription)
        return mobile.load_state()['devices'][self.peer]['push']['id']

    def test_status_exposes_only_public_key_and_private_state_is_owner_only(self):
        status = self.api('status')
        self.assertFalse(status['enabled']); self.assertTrue(status['available'])
        self.assertEqual(len(mobilepush.unb64(status['publicKey'])), 65)
        state = mobile.load_state()
        self.assertIn('PRIVATE KEY', state['push_key'])
        self.assertNotIn(state['push_key'], json.dumps(status))
        self.assertEqual(os.stat(mobile.STATE).st_mode & 0o777, 0o600)
        self.assertEqual(status['publicKey'], self.api('status')['publicKey'])
        self.enable()
        desktop = json.dumps(mobile.desktop_status())
        for secret in (state['push_key'], self.subscription['endpoint'], self.subscription['keys']['auth']):
            self.assertNotIn(secret, desktop)

    def test_only_exact_current_peer_grant_can_change_or_read_its_subscription(self):
        for grant in ('stale-grant', None):
            with self.assertRaises(PermissionError): mobilepush.action(self.peer, grant, {'action': 'status'})
        self.enable()
        self.assertFalse(mobilepush.action(self.other, 'other-grant', {'action': 'status'})['enabled'])
        with self.assertRaises(ValueError):
            mobilepush.action(self.other, 'other-grant', {'action': 'subscribe', 'subscription': self.subscription})
        with self.assertRaises(ValueError):
            mobilepush.action(self.other, 'other-grant', {'action': 'subscribe', 'subscription': {
                **self.subscription, 'endpoint': self.subscription['endpoint'].replace('web.push.apple.com', 'WEB.PUSH.APPLE.COM:443')}})
        self.api('unsubscribe')
        self.assertFalse(self.api('status')['enabled'])
        self.assertEqual(mobile.grant(self.peer), self.grant)

    def test_provider_allowlist_rejects_ssrf_userinfo_ports_fragments_and_bad_keys(self):
        for url in ('http://web.push.apple.com/opaque', 'https://127.0.0.1/opaque',
                    'https://web.push.apple.com.evil.test/opaque', 'https://evil.test/opaque',
                    'https://user@web.push.apple.com/opaque', 'https://web.push.apple.com:444/opaque',
                    'https://web.push.apple.com/opaque#secret', 'https://web.push.apple.com/opa\\que',
                    'https://web.push.apple.com/opa\nque'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.api('subscribe', subscription={**self.subscription, 'endpoint': url})
        for keys in ({'p256dh': 'bad', 'auth': 'bad'}, {'p256dh': mobilepush.b64(b'\x04' + b'\0' * 64), 'auth': self.subscription['keys']['auth']}):
            with self.assertRaises(ValueError): self.api('subscribe', subscription={**self.subscription, 'keys': keys})
        self.assertNotIn('push', mobile.load_state()['devices'][self.peer])

    def test_subscription_is_idempotent_and_disabling_does_not_remove_another_phone(self):
        identity = self.enable()
        self.assertEqual(identity, self.enable())
        other = {**self.subscription, 'endpoint': 'https://fcm.googleapis.com/fcm/send/' + 'y' * 40}
        mobilepush.action(self.other, 'other-grant', {'action': 'subscribe', 'subscription': other})
        self.api('unsubscribe')
        self.assertTrue(mobilepush.action(self.other, 'other-grant', {'action': 'status'})['enabled'])

    def test_delayed_unsubscribe_cannot_remove_replacement_even_with_same_browser_keys(self):
        old = self.enable()
        self.api('unsubscribe')
        replacement = self.enable()
        self.assertNotEqual(old, replacement)
        stale = self.api('unsubscribe', subscriptionId=old)
        self.assertTrue(stale['enabled'])
        self.assertEqual(stale['subscriptionId'], replacement)
        self.assertEqual(self.api('status')['subscriptionId'], replacement)
        self.assertFalse(self.api('unsubscribe', subscriptionId=replacement)['enabled'])

    def test_preference_change_or_revoke_during_http_preparation_cancels_before_send(self):
        import requests
        prepare = requests.Session.prepare_request
        response = requests.Response(); response.status_code = 201; response._content = b''
        for change in ('preferences', 'revoke'):
            with self.subTest(change=change):
                self.enable(); self.api('preferences', showTitles=True)
                identity = mobile.load_state()['devices'][self.peer]['push']['id']
                def before_send(session, request):
                    if change == 'preferences': self.api('preferences', showTitles=False)
                    else: mobile.revoke(self.peer)
                    return prepare(session, request)
                with mock.patch.object(requests.Session, 'prepare_request', autospec=True, side_effect=before_send), \
                        mock.patch.object(requests.Session, 'send', return_value=response) as network:
                    self.assertEqual(mobilepush.send(self.peer, self.grant, identity, session_title='Private session'), 0)
                    network.assert_not_called()

    def test_real_webpush_encrypts_only_generic_payload_and_forbids_proxy_redirects(self):
        import http_ece
        import requests
        identity = self.enable(); captured = {}
        with mobile.state_lock():
            state = mobile.load_state(); state['client'] = 'https://127.0.0.1:4443'; mobile.save_state(state)
        def post(session, url, **kwargs):
            captured.update(url=url, **kwargs)
            self.assertFalse(session.trust_env)
            result = requests.Response(); result.status_code = 201; result._content = b''; return result
        with mock.patch.object(requests.Session, 'post', autospec=True, side_effect=post):
            self.assertEqual(mobilepush.send(self.peer, self.grant, identity), 201)
        self.assertFalse(captured['allow_redirects'])
        self.assertEqual(captured['timeout'], (3, 7))
        self.assertEqual(captured['headers']['Content-Encoding'], 'aes128gcm')
        plaintext = http_ece.decrypt(captured['data'], private_key=self.key,
            auth_secret=mobilepush.unb64(self.subscription['keys']['auth']), version='aes128gcm')
        self.assertEqual(json.loads(plaintext), {'v': 1, 'kind': 'attention'})
        self.assertNotIn(self.peer.encode(), captured['data'])
        self.assertIn('vapid', captured['headers']['Authorization'].lower())

    def test_revoke_or_replacement_while_encryption_is_queued_prevents_network_dispatch(self):
        import requests
        identity = self.enable()
        original = pywebpush.webpush
        def revoke_before_send(*args, **kwargs):
            mobile.revoke(self.peer)
            return original(*args, **kwargs)
        with mock.patch.object(pywebpush, 'webpush', side_effect=revoke_before_send), mock.patch.object(requests.Session, 'post') as post:
            self.assertEqual(mobilepush.send(self.peer, self.grant, identity), 0)
            post.assert_not_called()

    def test_expired_subscription_is_deleted_but_replacement_survives_old_failure(self):
        import requests
        identity = self.enable()
        def expired(session, *args, **kwargs):
            result = requests.Response(); result.status_code = 410; result._content = b'private-provider-detail'; return result
        with mock.patch.object(requests.Session, 'post', autospec=True, side_effect=expired):
            self.assertEqual(mobilepush.send(self.peer, self.grant, identity), 410)
        self.assertFalse(self.api('status')['enabled'])
        identity = self.enable()
        def replaced(session, *args, **kwargs):
            self.api('unsubscribe'); self.enable()
            return expired(session)
        with mock.patch.object(requests.Session, 'post', autospec=True, side_effect=replaced):
            self.assertEqual(mobilepush.send(self.peer, self.grant, identity), 410)
        self.assertTrue(self.api('status')['enabled'])

    def test_test_send_is_explicit_bounded_and_rate_limited(self):
        self.enable()
        with mock.patch.object(mobilepush, 'send', return_value=201) as send:
            self.assertTrue(self.api('test')['sent'])
            with self.assertRaises(ValueError): self.api('test')
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.kwargs, {'kind': 'test'})

    def test_session_name_requires_per_phone_opt_in_and_never_includes_message(self):
        import http_ece
        import requests
        captured = []
        def post(session, url, **kwargs):
            captured.append(json.loads(http_ece.decrypt(kwargs['data'], private_key=self.key,
                auth_secret=b'fixture-auth-key', version='aes128gcm')))
            result = requests.Response(); result.status_code = 201; result._content = b''; return result
        def dispatch():
            identity = mobile.load_state()['devices'][self.peer]['push']['id']
            with mock.patch.object(requests.Session, 'post', autospec=True, side_effect=post):
                self.assertEqual(mobilepush.send(self.peer, self.grant, identity, session_title='HTTP\n\u202e\x00\u200b ' + 'x' * 160), 201)
        self.enable(); dispatch()
        self.assertEqual(captured[-1], {'v': 1, 'kind': 'attention'})
        self.api('preferences', showTitles=True); dispatch()
        self.assertEqual(set(captured[-1]), {'v', 'kind', 'session'})
        self.assertTrue(captured[-1]['session'].startswith('HTTP '))
        self.assertLessEqual(len(captured[-1]['session']), 100)
        self.assertNotIn('\u202e', captured[-1]['session'])
        self.assertNotIn('\x00', captured[-1]['session'])
        self.assertNotIn('\u200b', captured[-1]['session'])
        self.assertFalse(mobilepush.action(self.other, 'other-grant', {'action': 'status'})['showTitles'])
        self.api('preferences', showTitles=False); dispatch()
        self.assertNotIn('session', captured[-1])
        with self.assertRaises(ValueError): self.api('preferences', showTitles='true')

    def test_disabling_names_while_encryption_is_queued_cancels_private_dispatch(self):
        import requests
        self.enable(); self.api('preferences', showTitles=True)
        identity = mobile.load_state()['devices'][self.peer]['push']['id']
        original = pywebpush.webpush
        def turn_off_before_send(*args, **kwargs):
            self.api('preferences', showTitles=False)
            return original(*args, **kwargs)
        with mock.patch.object(pywebpush, 'webpush', side_effect=turn_off_before_send), mock.patch.object(requests.Session, 'post') as post:
            self.assertEqual(mobilepush.send(self.peer, self.grant, identity, session_title='Sensitive session'), 0)
            post.assert_not_called()


class NotificationSnapshotTest(unittest.TestCase):
    def test_snapshot_never_calls_gui_list_or_presence_and_contains_no_titles_or_payloads(self):
        manager = types.SimpleNamespace(lock=threading.RLock(), sessions={
            'one': types.SimpleNamespace(id='one', state='attention', kind='project', hidden=False, since=10),
            'hidden': types.SimpleNamespace(id='hidden', state='waiting', kind='project', hidden=True, since=11),
            'provider-1': types.SimpleNamespace(id='provider-1', state='waiting', kind='project', hidden=False, since=12)},
            pending={'p1': {'id': 'p1', 'session': 'one', 'payload': 'PRIVATE'},
                     'p2': {'id': 'p2', 'session': 'provider-1', 'payload': 'PRIVATE'}})
        self.assertEqual(mobilepush.snapshot(manager), {'sessions': [{'id': 'one', 'state': 'attention', 'since': 10, 'title': ''}], 'pending': ['p1'], 'pending_sessions': {'p1': 'one'}})

    def test_new_pending_card_uses_its_owner_name_instead_of_another_waiting_session(self):
        watch = mobilepush.Events()
        value = {'sessions': [{'id': 'a', 'state': 'waiting', 'since': 1, 'title': 'Old'},
                              {'id': 'b', 'state': 'attention', 'since': 2, 'title': 'HTTP'}],
                 'pending': [], 'pending_sessions': {}}
        self.assertFalse(watch.changed(value))
        self.assertTrue(watch.changed({**value, 'pending': ['card'], 'pending_sessions': {'card': 'b'}}))
        self.assertEqual(watch.session_title, 'HTTP')
        self.assertFalse(watch.changed(value))
        self.assertEqual(watch.session_title, '')

    def test_event_detection_suppresses_startup_and_unchanged_states_but_tracks_new_waits(self):
        watch = mobilepush.Events()
        def state(since=1, pending=()): return {'sessions': [{'id': 's', 'state': 'waiting', 'since': since}], 'pending': list(pending)}
        self.assertFalse(watch.changed(state()))
        self.assertFalse(watch.changed(state()))
        self.assertTrue(watch.changed(state(2)))
        self.assertTrue(watch.changed(state(2, ('card',))))
        self.assertFalse(watch.changed(state(2, ('card',))))
        self.assertFalse(watch.changed({'sessions': [], 'pending': []}))


@unittest.skipIf(pywebpush is None, 'optional Web Push runtime not installed')
class PushDispatcherTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_events_do_not_reset_provider_backoff_or_flood_the_same_phone(self):
        import asyncio
        clock = [0]
        ticks = [0]
        state = {'v': 1, 'devices': {'peer': {'grant': 'g', 'push': {'id': 'p'}}}}
        def events(_token):
            return {'sessions': [], 'pending': [str(ticks[0])]}
        async def pause(_seconds):
            clock[0] += 3; ticks[0] += 1
            if ticks[0] == 9: raise asyncio.CancelledError
        with mock.patch.object(mobile, 'load_state', return_value=state), \
                mock.patch.object(mobilepush, '_events', side_effect=events), \
                mock.patch.object(mobilepush, 'send', return_value=429) as send, \
                mock.patch.object(mobilepush, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(mobilepush.asyncio, 'sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError): await mobilepush.run('fixture-token')
        self.assertEqual(send.call_count, 1, 'new cards must not bypass the 30-second provider backoff')

    async def test_without_subscriptions_the_worker_never_polls_piperd(self):
        import asyncio
        with mock.patch.object(mobile, 'load_state', return_value={'devices': {}}), \
                mock.patch.object(mobilepush, '_events') as events, \
                mock.patch.object(mobilepush.asyncio, 'sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError): await mobilepush.run('fixture-token')
            events.assert_not_called()
