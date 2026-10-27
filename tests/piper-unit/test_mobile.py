"""Mobile invitations, peer authorization and real encrypted QUIC transport."""
import asyncio
import base64
import concurrent.futures
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import _env  # noqa: F401
import mobile


class MobileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = mock.patch.object(mobile, "STATE", os.path.join(self.temp.name, "mobile.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with mobile.state_lock():
            mobile.save_state({"v": 1, "endpoint": "11" * 32, "relay": "https://relay.example",
                               "devices": {}})

    def code(self):
        url = mobile.invitation("https://mobile.example")
        encoded = url.split("#iroh=")[1]
        ticket = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(ticket["endpoint"], "11" * 32)
        with open(mobile.STATE) as f:
            self.assertNotIn(ticket["code"], f.read())
        self.assertEqual(os.stat(mobile.STATE).st_mode & 0o777, 0o600)
        self.assertNotIn("?", url)
        return ticket["code"]

    def test_pairing_is_single_use_and_binds_authenticated_key(self):
        code = self.code()
        self.assertFalse(mobile.pair("22" * 32, "wrong"))
        self.assertIsNone(mobile.grant("22" * 32))
        self.assertTrue(mobile.pair("22" * 32, code))
        self.assertIsNotNone(mobile.grant("22" * 32))
        self.assertFalse(mobile.pair("33" * 32, code))
        self.assertTrue(mobile.revoke("22" * 32))
        self.assertIsNone(mobile.grant("22" * 32))
        self.assertFalse(mobile.pair("22" * 32, code))

    def test_desktop_status_never_exposes_credentials_and_detects_bridge_exit(self):
        code = self.code()
        self.assertTrue(mobile.pair("22" * 32, code))
        with mobile.state_lock():
            state = mobile.load_state()
            state["key"] = "private-computer-key"
            state["relay"] = "https://relay.example?token=" + "x" * 43
            mobile.save_state(state)
        self.assertFalse(mobile.desktop_status()["ready"])
        with mobile.runtime_lock() as running:
            self.assertFalse(mobile.desktop_status()["ready"])
            running.write("ready")
            running.flush()
            status = mobile.desktop_status()
            self.assertTrue(status["ready"])
            with self.assertRaises(ValueError), mobile.runtime_lock():
                pass
        self.assertFalse(mobile.desktop_status()["ready"])
        encoded = json.dumps(status)
        for secret in ("private-computer-key", "x" * 43, mobile.grant("22" * 32), code):
            self.assertNotIn(secret, encoded)
        self.assertEqual(status["devices"][0]["id"], "22" * 32)

    def test_installation_link_is_public_and_never_creates_an_invitation(self):
        with mobile.state_lock():
            state = mobile.load_state()
            state["client"] = "https://Mobile.Example/"
            mobile.save_state(state)
        status = mobile.desktop_status()
        self.assertEqual(status["install_url"], "https://mobile.example/")
        self.assertIsNone(status["pending"])
        self.assertNotIn("pending", mobile.load_state())
        with mobile.state_lock():
            state = mobile.load_state()
            state["client"] = "https://mobile.example?token=private"
            mobile.save_state(state)
        with self.assertRaises(ValueError):
            mobile.desktop_status()

    def test_desktop_invitation_cancellation_is_bound_to_its_dialog(self):
        with mobile.runtime_lock() as running:
            running.write("ready")
            running.flush()
            first = mobile.desktop_invitation("First phone")
            second = mobile.desktop_invitation("Second phone")
            self.assertFalse(mobile.cancel_invitation(first["id"]))
            self.assertEqual(mobile.desktop_status()["pending"]["id"], second["id"])
            self.assertTrue(mobile.cancel_invitation(second["id"]))
            ticket = json.loads(base64.urlsafe_b64decode(second["url"].split("#iroh=")[1] + "==="))
            self.assertFalse(mobile.pair("22" * 32, ticket["code"]))
            third = mobile.desktop_invitation("iPhone")
            ticket = json.loads(base64.urlsafe_b64decode(third["url"].split("#iroh=")[1] + "==="))
            self.assertTrue(mobile.pair("33" * 32, ticket["code"]))
            self.assertEqual(mobile.desktop_status()["last_pairing"], third["id"])
            self.assertIsNone(mobile.desktop_status()["pending"])
        with self.assertRaises(ValueError):
            mobile.desktop_invitation("offline")

    def test_expiry_and_replacing_an_invitation(self):
        code = self.code()
        with mock.patch.object(mobile.time, "time", return_value=mobile.time.time() + 61):
            self.assertFalse(mobile.pair("22" * 32, code))
        new = self.code()
        self.assertFalse(mobile.pair("22" * 32, code))
        self.assertTrue(mobile.pair("22" * 32, new))

    def test_concurrent_pairing_has_exactly_one_winner(self):
        code = self.code()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            answers = list(pool.map(lambda n: mobile.pair("%064x" % n, code), range(1, 9)))
        self.assertEqual(sum(answers), 1)
        self.assertEqual(len(mobile.load_state()["devices"]), 1)

    def test_only_explicit_https_origins(self):
        for bad in ("http://relay.example", "https://u:p@relay.example", "https://relay.example/path",
                    "https://relay.example/?code=secret", "https://relay.example/#fragment",
                    "https://relay.example\r\n", "//relay.example", "https://relay.example:bad",
                    "https://relay.example\\", "https://relay.example;policy"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                mobile.https_url(bad)
        self.assertEqual(mobile.https_url("https://relay.example/"), "https://relay.example")
        self.assertEqual(mobile.https_url("https://Relay.Example:443/"), "https://relay.example")
        self.assertEqual(mobile.https_url("https://[::1]:9443/"), "https://[::1]:9443")

    def test_private_relay_capability_is_separate_from_public_origin(self):
        token = "x" * 43
        self.assertEqual(mobile.relay_url("https://Relay.Example:443/?token=" + token),
                         "https://relay.example?token=" + token)
        for query in ("token=short", "token=" + token + "&other=1", "token=" + token + "&token=" + token,
                      "other=" + token, "token=%78" + "x" * 42):
            with self.subTest(query=query), self.assertRaises(ValueError):
                mobile.relay_url("https://relay.example?" + query)
        with self.assertRaises(ValueError):
            mobile.https_url("https://relay.example?token=" + token)
        secret_file = Path(self.temp.name) / "relay.token"
        secret_file.write_text(token + "\n")
        secret_file.chmod(0o600)
        self.assertEqual(mobile.relay_token(secret_file), token)
        secret_file.chmod(0o644)
        with self.assertRaises(ValueError):
            mobile.relay_token(secret_file)

    def test_proxy_target_is_fixed_and_security_headers_are_not_forwarded(self):
        connection = mock.MagicMock()
        response = connection.getresponse.return_value
        response.status, response.read.return_value = 200, b"response"
        response.getheaders.return_value = [("Content-Type", "application/json"), ("Set-Cookie", "private")]
        with mock.patch.object(mobile.http.client, "HTTPConnection", return_value=connection) as upstream:
            meta, body = mobile.proxy_http({"method": "POST", "path": "/api/test",
                "headers": {"Host": "evil.example", "Authorization": "untrusted", "Cookie": "private",
                            "Content-Type": "application/json", "X-Piper-Environment": "server"}}, b"{}", "test-token", lambda: True)
        upstream.assert_called_once_with("127.0.0.1", mobile.PORT, timeout=120)
        self.assertEqual(connection.request.call_args.args, ("POST", "/api/test"))
        headers = connection.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-token")
        self.assertNotIn("Host", headers)
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Set-Cookie", meta["headers"])
        self.assertEqual(body, b"response")
        connection.request.assert_called_once()  # no retry on mutations

    def test_database_proxy_deadline_covers_remote_ssh_and_maximum_gui_query(self):
        connection = mock.MagicMock()
        response = connection.getresponse.return_value
        response.status, response.read.return_value = 200, b'{}'
        response.getheaders.return_value = []
        with mock.patch.object(mobile.http.client, 'HTTPConnection', return_value=connection) as upstream:
            mobile.proxy_http({'method': 'POST', 'path': '/api/db/query',
                               'headers': {'X-Piper-Environment': 'server'}}, b'{}', 'test-token', lambda: True)
        self.assertGreater(upstream.call_args.kwargs['timeout'], 2 * 3600 + 180 + 7)
        connection.request.assert_called_once()

    def test_invalid_paths_methods_and_header_injection(self):
        for path in ("http://evil.example/api/state", "//evil.example/api/state", "/piper/index.html",
                     "/api/state\r\nHost: evil", "/api/a\\b", "/api/x#secret"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                mobile.request_target({"path": path})
        with self.assertRaises(ValueError):
            mobile.request_target({"path": "/api/state", "method": "CONNECT"})
        with self.assertRaises(ValueError):
            mobile.request_target({"path": "/api/state", "headers": {"Content-Type": "x\r\nHost: evil"}})

    def test_cancel_during_connect_cannot_dispatch_a_mutation(self):
        cancellation = mobile._HttpCancellation()
        connection = mock.MagicMock()
        connection.connect.side_effect = cancellation.cancel
        with mock.patch.object(mobile.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaises(PermissionError):
                mobile.proxy_http({'method': 'POST', 'path': '/api/fs/delete'}, b'{}',
                                  'test-token', lambda: True, cancellation)
        connection.request.assert_not_called()
        connection.close.assert_called_once()

    def test_http_compression_is_explicit_and_preserves_legacy_clients(self):
        import gzip
        raw = ('{"text":"' + 'Konverzace 🌲 ' * 1000 + '"}').encode()
        connection = mock.MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        for requested in (False, True):
            response.read.return_value = gzip.compress(raw) if requested else raw
            response.getheaders.return_value = [('Content-Type', 'application/json')] + (
                [('Content-Encoding', 'gzip')] if requested else [])
            value = dict(path='/api/state', **({'accept_encoding': 'gzip'} if requested else {}))
            with mock.patch.object(mobile.http.client, 'HTTPConnection', return_value=connection):
                meta, body = mobile.proxy_http(value, b'', 'test-token', lambda: True)
            self.assertEqual(connection.request.call_args.kwargs['headers'].get('Accept-Encoding'),
                             'gzip' if requested else None)
            self.assertEqual(meta.get('encoding'), 'gzip' if requested else None)
            self.assertEqual(gzip.decompress(body) if requested else body, raw)
            self.assertNotIn('Content-Encoding', meta['headers'])
        for value in ('br', ['gzip'], 'gzip\r\nHost: evil'):
            with self.assertRaises(ValueError):
                mobile.request_target(dict(path='/api/state', accept_encoding=value))
        response.getheaders.return_value = [('Content-Encoding', 'br')]
        with mock.patch.object(mobile.http.client, 'HTTPConnection', return_value=connection), self.assertRaises(ValueError):
            mobile.proxy_http(dict(path='/api/state', accept_encoding='gzip'), b'', 'test-token', lambda: True)

    def test_corrupted_state_is_not_a_new_pairing_store(self):
        with open(mobile.STATE, "w") as f:
            f.write('{"v":2,"devices":[]}')
        with self.assertRaises(ValueError):
            mobile.grant("11" * 32)

    def test_shell_cli_lists_no_private_grants_and_revokes_one_device(self):
        self.assertTrue(mobile.pair("22" * 32, self.code()))
        private = mobile.grant("22" * 32)
        script = Path(__file__).resolve().parents[2] / "scripts" / "piper.sh"
        env = dict(os.environ, PIPER_HOME=self.temp.name, PIPER_MOBILE_PYTHON=sys.executable)
        listed = subprocess.run(["sh", str(script), "mobile", "devices"], env=env,
                                check=True, capture_output=True, text=True)
        self.assertIn("22" * 32, json.loads(listed.stdout))
        self.assertNotIn(private, listed.stdout)
        revoked = subprocess.run(["sh", str(script), "mobile", "revoke", "22" * 32], env=env,
                                 check=True, capture_output=True, text=True)
        self.assertIn("Device revoked", revoked.stdout)
        self.assertIsNone(mobile.grant("22" * 32))

    def test_export_contains_only_public_assets_and_pins_relay_in_csp(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "piper-mobile-export.py"
        spec = importlib.util.spec_from_file_location("mobile_export", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = Path(self.temp.name) / "source"
        web = source / "piper" / "web"
        (web / "dist").mkdir(parents=True)
        (web / "vendor" / "iroh").mkdir(parents=True)
        (web / "vendor" / "iroh" / "piper_mobile_bg.wasm").write_bytes(b"wasm")
        (source / "secret-token").write_text("not public")
        (web / "themes.json").write_text('{}')
        for name in ("app.css", "mobile.css", "manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png", "mobile-sw.js"):
            (web / name).write_bytes(b"public")
        (web / "index.html").write_text('<head><script type="importmap" nonce="<!--PIPER_NONCE-->">{}</script>'
            '<!--PIPER_BOOT--><script type="module" src="/piper/dist/main.js"></script></head>')
        destination = Path(self.temp.name) / "export"
        with mock.patch.object(module, "SK", source):
            module.export(destination, "https://relay.example")
            page = (destination / "index.html").read_text()
            self.assertIn("wss://relay.example", page)
            self.assertIn("'wasm-unsafe-eval'".replace("'", "&#x27;"), page)
            self.assertNotIn("<!--PIPER_", page)
            self.assertIn("/piper/dist/mobileboot.js", page)
            self.assertFalse((destination / "secret-token").exists())
            self.assertEqual((destination / "service-worker.js").read_bytes(), b"public")
            self.assertIn("worker-src 'self'", (destination / "_headers").read_text())
            self.assertIn("frame-ancestors 'none'", (destination / "_headers").read_text())
            with self.assertRaises(ValueError):
                module.export(destination, "https://relay.example")


try:
    import iroh
except ImportError:
    iroh = None


@unittest.skipIf(iroh is None, "optional iroh runtime not installed")
class MobileQuicTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = mock.patch.object(mobile, "STATE", os.path.join(self.temp.name, "mobile.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.server = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_minimal(),
            bind_addr="127.0.0.1:0", alpns=[mobile.ALPN], relay_mode=iroh.RelayMode.disabled()))
        self.client = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_minimal(),
            bind_addr="127.0.0.1:0", relay_mode=iroh.RelayMode.disabled()))
        self.addAsyncCleanup(self.server.close)
        self.addAsyncCleanup(self.client.close)
        with mobile.state_lock():
            mobile.save_state({"v": 1, "endpoint": self.server.id().to_bytes().hex(),
                               "relay": "https://relay.example", "devices": {}})
        self.tasks = []
        self.accept_task = asyncio.create_task(self.accept())
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        self.accept_task.cancel()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(self.accept_task, *self.tasks, return_exceptions=True)

    async def accept(self):
        await mobile.serve_endpoint(self.server, "test-token")

    async def connection(self):
        addr = iroh.EndpointAddr(self.server.id(), None, self.server.bound_sockets())
        return await asyncio.wait_for(self.client.connect(addr, mobile.ALPN), 5)

    async def request(self, conn, header, body=b""):
        bi = await conn.open_bi()
        await bi.send().write_all(mobile.header_bytes({"v": 1, **header}) + body)
        await bi.send().finish()
        meta = await asyncio.wait_for(mobile.read_header(bi.recv()), 5)
        data = await bi.recv().read_to_end(mobile.MAX_RESPONSE)
        return meta, data

    async def test_real_tls_peer_pairing_request_and_idle_revocation(self):
        url = mobile.invitation("https://mobile.example")
        encoded = url.split("#iroh=")[1]
        code = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))["code"]
        conn = await self.connection()
        meta, _ = await self.request(conn, {"op": "pair", "code": code, "peer": "ff" * 32})
        self.assertEqual(meta["status"], 200)
        peer = self.client.id().to_bytes().hex()
        self.assertIsNotNone(mobile.grant(peer))
        self.assertIsNone(mobile.grant("ff" * 32))
        with mock.patch.object(mobile, "proxy_http", return_value=({"v": 1, "status": 200}, b"encrypted-response")):
            meta, body = await self.request(conn, {"op": "http", "path": "/api/state"})
        self.assertEqual((meta["status"], body), (200, b"encrypted-response"))
        mobile.revoke(peer)
        await asyncio.wait_for(conn.closed(), 3)

    async def test_stream_saturation_refuses_only_the_extra_request_not_the_connection(self):
        invitation = mobile.invitation("https://mobile.example").split("#iroh=")[1]
        code = json.loads(base64.urlsafe_b64decode(invitation + "=" * (-len(invitation) % 4)))["code"]
        self.assertTrue(mobile.pair(self.client.id().to_bytes().hex(), code))
        conn = await self.connection()
        accepted = asyncio.Event()
        count = 0
        read_header = mobile.read_header

        async def observed(recv):
            nonlocal count
            value = await read_header(recv)
            if value.get("op") == "http":
                count += 1
                if count == 16:
                    accepted.set()
            return value

        async def drain(bi):
            await bi.send().finish()
            meta = await asyncio.wait_for(read_header(bi.recv()), 3)
            await bi.recv().read_to_end(1000)
            self.assertEqual(meta["status"], 200)

        with mock.patch.object(mobile, "read_header", side_effect=observed), mock.patch.object(
                mobile, "proxy_http", return_value=({"v": 1, "status": 200}, b"ok")) as proxy:
            held = []
            for _ in range(16):
                bi = await conn.open_bi()
                await bi.send().write_all(mobile.header_bytes({"v": 1, "op": "http", "path": "/api/ping"}))
                held.append(bi)
            await asyncio.wait_for(accepted.wait(), 3)
            meta, _ = await self.request(conn, {"op": "hello"})
            self.assertEqual(meta["status"], 503)
            await asyncio.gather(*(drain(bi) for bi in held))
            self.assertEqual(proxy.call_count, 16)
            await asyncio.sleep(.05)  # completed stream tasks are removed by their done callbacks
            meta, _ = await self.request(conn, {"op": "hello"})
            self.assertEqual(meta["status"], 200)

    async def test_cancelled_streams_release_capacity_without_reconnecting(self):
        self.authorize_client()
        conn = await self.connection()
        held = []
        read_header = mobile.read_header
        accepted = asyncio.Event()
        count = 0

        async def observed(recv):
            nonlocal count
            value = await read_header(recv)
            if value.get('op') == 'http':
                count += 1
                if count == 16:
                    accepted.set()
            return value

        with mock.patch.object(mobile, 'read_header', side_effect=observed), mock.patch.object(
                mobile, 'proxy_http', return_value=({'v': 1, 'status': 200}, b'ok')):
            try:
                for _ in range(16):
                    bi = await conn.open_bi()
                    await bi.send().write_all(mobile.header_bytes({'v': 1, 'op': 'http', 'path': '/api/ping'}))
                    held.append(bi)
                await asyncio.wait_for(accepted.wait(), 3)
                for bi in held:
                    await bi.recv().stop(0)
                await asyncio.sleep(.1)
                meta, _ = await self.request(conn, {'op': 'hello'})
                self.assertEqual(meta['status'], 200, 'cancelled requests still occupy the connection budget')
            finally:
                for bi in held:
                    try:
                        await bi.send().finish()
                    except Exception:
                        pass

    async def test_cancelled_http_releases_the_upstream_worker(self):
        await self.cancelled_http_worker()

    async def test_cancelled_http_body_releases_the_upstream_worker(self):
        await self.cancelled_http_worker(body=True)

    async def cancelled_http_worker(self, body=False):
        self.authorize_client()
        conn = await self.connection()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        started, disconnected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        handlers = set()

        async def upstream(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                if request.startswith(b'GET /api/state '):
                    if body:
                        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n')
                        await writer.drain()
                    started.set()
                    eof = asyncio.create_task(reader.read())
                    released = asyncio.create_task(release.wait())
                    try:
                        await asyncio.wait([eof, released], return_when=asyncio.FIRST_COMPLETED)
                        if eof.done():
                            disconnected.set()
                    finally:
                        eof.cancel(); released.cancel()
                        await asyncio.gather(eof, released, return_exceptions=True)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                await writer.drain()
            finally:
                writer.close()
                handlers.discard(task)

        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        try:
            with mock.patch.object(mobile, 'PORT', server.sockets[0].getsockname()[1]):
                bi = await conn.open_bi()
                await bi.send().write_all(mobile.header_bytes({'v': 1, 'op': 'http', 'path': '/api/state'}))
                await bi.send().finish()
                await asyncio.wait_for(started.wait(), 3)
                await bi.recv().stop(0)
                await asyncio.wait_for(disconnected.wait(), 1)
                meta, _ = await asyncio.wait_for(self.request(conn, {'op': 'http', 'path': '/api/ping'}), 2)
                self.assertEqual(meta['status'], 200)
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            await asyncio.gather(*handlers, return_exceptions=True)

    async def test_push_administration_uses_only_authenticated_peer_and_current_grant(self):
        import mobilepush
        self.authorize_client()
        expected = mobile.grant(self.client.id().to_bytes().hex())
        conn = await self.connection()
        with mock.patch.object(mobilepush, 'action', return_value={'available': True, 'enabled': False, 'publicKey': 'public-only'}) as action:
            meta, body = await self.request(conn, {'op': 'push', 'action': 'status', 'peer': 'ff' * 32})
            self.assertEqual(meta['status'], 200)
            self.assertEqual(json.loads(body)['publicKey'], 'public-only')
            self.assertEqual(action.call_args.args[:2], (self.client.id().to_bytes().hex(), expected))
        mobile.revoke(self.client.id().to_bytes().hex())
        await asyncio.wait_for(conn.closed(), 3)

    async def test_unpaired_peer_never_reaches_loopback(self):
        conn = await self.connection()
        with mock.patch.object(mobile, "proxy_http") as proxy:
            meta, _ = await self.request(conn, {"op": "http", "path": "/api/state"})
        self.assertEqual(meta["status"], 403)
        proxy.assert_not_called()

    def authorize_client(self):
        encoded = mobile.invitation("https://mobile.example").split("#iroh=")[1]
        code = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))["code"]
        self.assertTrue(mobile.pair(self.client.id().to_bytes().hex(), code))

    async def test_waiting_http_request_cannot_dispatch_after_revocation(self):
        await self.queued_http_revocation()

    async def test_cancelled_queued_mutation_never_dispatches(self):
        self.authorize_client()
        conn = await self.connection()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        started, release, queued = threading.Event(), threading.Event(), asyncio.Event()

        def block_worker():
            started.set()
            release.wait(5)

        busy = loop.run_in_executor(None, block_worker)
        while not started.is_set():
            await asyncio.sleep(.001)
        original = asyncio.to_thread

        async def observe_queue(func, *args):
            if func is mobile.proxy_http:
                queued.set()
            return await original(func, *args)

        try:
            with mock.patch.object(mobile.asyncio, 'to_thread', side_effect=observe_queue), mock.patch.object(
                    mobile.http.client, 'HTTPConnection') as upstream:
                bi = await conn.open_bi()
                await bi.send().write_all(mobile.header_bytes({'v': 1, 'op': 'http', 'method': 'POST',
                                                             'path': '/api/fs/delete'}) + b'{}')
                await bi.send().finish()
                await asyncio.wait_for(queued.wait(), 2)
                await bi.recv().stop(0)
                await asyncio.sleep(.05)
                release.set()
                await busy
                await loop.run_in_executor(None, lambda: None)  # queued work has now drained
                upstream.return_value.request.assert_not_called()
        finally:
            release.set()
            await busy

    async def test_repairing_cannot_revive_a_queued_request_from_the_old_grant(self):
        await self.queued_http_revocation(repair=True)

    async def queued_http_revocation(self, repair=False):
        self.authorize_client()
        conn = await self.connection()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        started, release, queued = threading.Event(), threading.Event(), asyncio.Event()

        def block_worker():
            started.set()
            release.wait(10)
        busy = loop.run_in_executor(None, block_worker)
        while not started.is_set():
            await asyncio.sleep(.001)
        original = asyncio.to_thread

        async def observe_queue(func, *args):
            if func is mobile.proxy_http:
                queued.set()
            return await original(func, *args)
        request, pairing = None, None
        release_pair = asyncio.Event()
        try:
            with mock.patch.object(mobile.asyncio, "to_thread", side_effect=observe_queue), \
                    mock.patch.object(mobile.http.client, "HTTPConnection") as upstream:
                request = asyncio.create_task(self.request(conn,
                    {"op": "http", "method": "POST", "path": "/api/fs/delete"}, b'{}'))
                await asyncio.wait_for(queued.wait(), 2)
                if repair:
                    encoded = mobile.invitation("https://mobile.example").split("#iroh=")[1]
                    code = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))["code"]
                    accepted = asyncio.Event()
                    read_header = mobile.read_header

                    async def pause_pair(recv):
                        value = await read_header(recv)
                        if value.get("op") == "pair":
                            accepted.set()
                            await release_pair.wait()
                        return value
                    with mock.patch.object(mobile, "read_header", side_effect=pause_pair):
                        pairing = asyncio.create_task(self.request(conn, {"op": "pair", "code": code}))
                        await asyncio.wait_for(accepted.wait(), 2)
                        mobile.revoke(self.client.id().to_bytes().hex())
                        release_pair.set()
                        response, _ = await pairing
                    self.assertEqual(response["status"], 200)
                else:
                    mobile.revoke(self.client.id().to_bytes().hex())
                release.set()  # deliberately before the one-second connection watcher runs
                meta, _ = await asyncio.wait_for(request, 2)
                self.assertEqual(meta["status"], 403)
                upstream.return_value.request.assert_not_called()
        finally:
            release.set()
            release_pair.set()
            await busy
            for pending in (request, pairing):
                if pending is not None:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    async def test_unpaired_waits_do_not_use_the_paired_connection_budget(self):
        self.authorize_client()
        clients, held = [], []
        try:
            for _ in range(mobile.MAX_UNPAIRED_CONNECTIONS):
                client = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_minimal(),
                    bind_addr="127.0.0.1:0", relay_mode=iroh.RelayMode.disabled()))
                clients.append(client)
                address = iroh.EndpointAddr(self.server.id(), None, self.server.bound_sockets())
                connection = await asyncio.wait_for(client.connect(address, mobile.ALPN), 3)
                held.append(connection)
                meta, _ = await self.request(connection, {"op": "hello"})
                self.assertEqual(meta["status"], 200)
            overflow = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_minimal(),
                bind_addr="127.0.0.1:0", relay_mode=iroh.RelayMode.disabled()))
            clients.append(overflow)
            address = iroh.EndpointAddr(self.server.id(), None, self.server.bound_sockets())
            refused = await asyncio.wait_for(overflow.connect(address, mobile.ALPN), 3)
            await asyncio.wait_for(refused.closed(), 2)
            authorized = await self.connection()
            with mock.patch.object(mobile, "proxy_http", return_value=({"v": 1, "status": 200}, b"available")):
                meta, body = await self.request(authorized, {"op": "http", "path": "/api/state"})
            self.assertEqual((meta["status"], body), (200, b"available"))
        finally:
            await asyncio.gather(*(client.close() for client in clients))

    async def test_pairing_releases_its_unpaired_connection_slot(self):
        with mock.patch.object(mobile, "MAX_UNPAIRED_CONNECTIONS", 1):
            encoded = mobile.invitation("https://mobile.example").split("#iroh=")[1]
            code = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))["code"]
            connection = await self.connection()
            meta, _ = await self.request(connection, {"op": "pair", "code": code})
            self.assertEqual(meta["status"], 200)
            other = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_minimal(),
                bind_addr="127.0.0.1:0", relay_mode=iroh.RelayMode.disabled()))
            try:
                address = iroh.EndpointAddr(self.server.id(), None, self.server.bound_sockets())
                newcomer = await asyncio.wait_for(other.connect(address, mobile.ALPN), 3)
                meta, body = await self.request(newcomer, {"op": "hello"})
                self.assertEqual(meta["status"], 200)
                self.assertFalse(json.loads(body)["paired"])
            finally:
                await other.close()

    async def test_wrong_pinned_computer_key_cannot_connect(self):
        other = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_minimal(),
            bind_addr="127.0.0.1:0", relay_mode=iroh.RelayMode.disabled()))
        try:
            wrong = iroh.EndpointAddr(other.id(), None, self.server.bound_sockets())
            with self.assertRaises(Exception):
                await asyncio.wait_for(self.client.connect(wrong, mobile.ALPN), 2)
            self.assertEqual(mobile.load_state()["devices"], {})
        finally:
            await other.close()

    async def test_real_terminal_binary_input_and_live_revocation(self):
        from websockets.asyncio.server import serve
        received = []

        async def terminal(ws):
            received.append(json.loads(await ws.recv()))
            await ws.send(b"terminal replay")
            async for message in ws:
                received.append(message)
                await ws.send(message)

        async with serve(terminal, "127.0.0.1", 0) as upstream:
            port = upstream.sockets[0].getsockname()[1]
            with mock.patch.object(mobile, "PORT", port):
                encoded = mobile.invitation("https://mobile.example").split("#iroh=")[1]
                code = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))["code"]
                self.assertTrue(mobile.pair(self.client.id().to_bytes().hex(), code))
                conn = await self.connection()
                bi = await conn.open_bi()
                await bi.send().write_all(mobile.header_bytes({"v": 1, "op": "ws", "path": "/ws/test"}))
                self.assertEqual((await mobile.read_header(bi.recv()))["status"], 101)
                self.assertEqual(await mobile.read_frame(bi.recv()), (2, b"terminal replay"))
                self.assertEqual(received[0]["auth"], "test-token")
                await mobile.write_frame(bi.send(), 1, b'{"input":"hello"}')
                self.assertEqual(await mobile.read_frame(bi.recv()), (1, b'{"input":"hello"}'))
                await mobile.write_frame(bi.send(), 2, bytes([0, 255, 27]))
                self.assertEqual(await mobile.read_frame(bi.recv()), (2, bytes([0, 255, 27])))
                mobile.revoke(self.client.id().to_bytes().hex())
                await asyncio.wait_for(conn.closed(), 3)
                self.assertEqual(len(received), 3)

    async def test_large_terminal_replay_is_chunked_without_losing_order_or_bytes(self):
        from websockets.asyncio.server import serve
        replay = ((b"\x1b[32m" + "größenwahn".encode() + b"\x1b[0m\r\n") * 400000)[:8 << 20]
        self.assertGreater(len(replay), mobile.MAX_FRAME)

        async def terminal(ws):
            await ws.recv()  # local bridge authentication
            await ws.send(replay)
            await ws.send('{"t":"replay-end"}')
            await ws.wait_closed()

        self.authorize_client()
        async with serve(terminal, "127.0.0.1", 0) as upstream:
            with mock.patch.object(mobile, "PORT", upstream.sockets[0].getsockname()[1]):
                conn = await self.connection()
                bi = await conn.open_bi()
                await bi.send().write_all(mobile.header_bytes({"v": 1, "op": "ws", "path": "/ws/test"}))
                self.assertEqual((await mobile.read_header(bi.recv()))["status"], 101)
                parts = []
                while True:
                    kind, data = await asyncio.wait_for(mobile.read_frame(bi.recv()), 5)
                    if kind == 1:
                        self.assertEqual(json.loads(data)["t"], "replay-end")
                        break
                    self.assertEqual(kind, 2)
                    self.assertLessEqual(len(data), 65536)
                    parts.append(data)
                self.assertEqual(b"".join(parts), replay)
                await mobile.write_frame(bi.send(), 8, b"")
                meta, _ = await self.request(conn, {"op": "hello"})
                self.assertEqual(meta["status"], 200)

    async def test_production_endpoint_never_opens_a_lan_udp_listener(self):
        import ipaddress
        endpoint = await mobile.mobile_endpoint(iroh, os.urandom(32), "https://127.0.0.1:9")
        try:
            sockets = endpoint.bound_sockets()
            self.assertTrue(sockets)
            for address in sockets:
                self.assertTrue(ipaddress.ip_address(address.rsplit(":", 1)[0].strip("[]")).is_loopback)
        finally:
            await endpoint.close()
