"""Credential-free Linux process ownership bootstrap for the pinned SDK.

Executed as a script before oto-sandbox-net; never imported by the runtime.
The private handshake directory is not mounted into the agent sandbox.
"""

import argparse
import ctypes
import json
import os
from pathlib import Path
import signal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ownership-file', required=True)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        raise SystemExit(2)
    parent = os.getppid()
    os.setsid()
    # Preserve the parent-death chain even before the network launcher execs.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise SystemExit(1)
    fields = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    record = {'nonce': args.nonce, 'pid': os.getpid(), 'start_ticks': int(fields[19]),
              'session_id': os.getsid(0)}
    fd = os.open(args.ownership_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream)
    # oto-sandbox-net launches a Python route shim before entering bwrap's
    # mount namespace. Neither a writable cwd nor HOME may inject Python code.
    os.environ['PYTHONNOUSERSITE'] = '1'
    os.environ['PYTHONSAFEPATH'] = '1'
    os.execv(command[0], command)


if __name__ == '__main__':
    main()
