#!/usr/bin/env python3
"""Optional notebook tools through the session mailbox; capabilities stay on the server.

The controller forwards once through its private reverse SSH tunnel. The container
needs no network exception and cannot select a different provider or server session.
"""
import importlib.util
import os
import sys


def main():
    spec = importlib.util.spec_from_file_location('piper_local_protocol', os.path.join(os.path.dirname(__file__), 'piper-mcp.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    call = module.call
    # The outer operation is deliberately not replayable, including when the
    # inner tool reads: a controller restart must never resubmit an uncertain call.
    module.CONFIRMED.add('local_provider')
    module.call = lambda tool, args: call('local_provider', {'tool': tool, 'args': args})
    sys.argv += ['--role', 'local-provider']
    module.main()


if __name__ == '__main__':
    main()
