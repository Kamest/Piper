"""Private launcher owned by piper-sandbox.py, never a host command escape hatch.

macOS cannot apply Seatbelt twice. Native HTTP children therefore receive their own
stricter profile from the wrapper, rather than inheriting its broader test profile.
Only the wrapper's preselected Java/ijhttp and paths below its WORK can be used.
The broker performs no writes to client-supplied filesystem paths.
"""
import base64
import json
import os
import socket
import subprocess
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'piper'))
import httpjail
import httpcompat


def start(work, env):
    binary = env.get('PIPER_IJHTTP')
    if not binary:
        return None
    java, lib = httpjail.runtime(binary, env)
    # Prepared before untrusted tests start; the wrapper denies writes to this
    # fixed cache, including its directory entry. Never a client-provided jar.
    compat = httpcompat.install(lib, os.path.join(work, 'http-native-compat'))
    endpoint = os.path.join(work, 'http-broker.sock')
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(endpoint)
    os.chmod(endpoint, 0o600)
    listener.listen(8)
    stopping = threading.Event()
    active = threading.BoundedSemaphore(4)
    pending = threading.BoundedSemaphore(8)

    def request(client):
        try:
            client.settimeout(3610)
            with client.makefile('rb') as stream:
                spec = json.loads(stream.readline(2 << 20))
            args = httpjail.validate_test_request(spec, work)
            if not active.acquire(blocking=False):
                raise httpjail.IsolationError('HTTP test broker concurrency limit')
            try:
                def stdout(data):
                    client.sendall(json.dumps({'stdout': base64.b64encode(data).decode('ascii')}).encode() + b'\n')
                result = httpjail.execute(java, lib, *args, test_root=work, compat=compat, on_stdout=stdout)
                response = dict(rc=result.returncode, out=base64.b64encode(result.stdout).decode('ascii'),
                                err=base64.b64encode(result.stderr).decode('ascii'), binary_safe=result.http_binary_safe)
            finally:
                active.release()
        except Exception as error:
            response = dict(error='isolated HTTP test launch failed: %s' % error,
                            timeout=isinstance(error, subprocess.TimeoutExpired),
                            output_limit=isinstance(error, httpjail.OutputLimit))
        try:
            client.sendall(json.dumps(response).encode() + b'\n')
        except OSError:
            pass
        client.close()
        pending.release()

    def accept():
        while not stopping.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                return
            if not pending.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=request, args=(client,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return endpoint, listener, stopping
