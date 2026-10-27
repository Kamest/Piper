"""Test-only transport substitute; production never imports this module."""
import os
import subprocess
import json


def run(binary, work, args, env, timeout, proxy_port, **kwargs):
    # Unit/e2e orchestration tests exercise result handling; the dedicated native
    # suite exercises the real OS jail. This process still inherits the mandatory
    # test sandbox and contains only synthetic fixture data.
    result = subprocess.run([binary, *args], cwd=work, env=dict(os.environ), timeout=timeout,
                            stdin=subprocess.DEVNULL, capture_output=True)
    with open(os.environ['FAKE_IJHTTP']) as fixture:
        result.http_binary_safe = json.load(fixture).get('binary_safe') is True
    return result
