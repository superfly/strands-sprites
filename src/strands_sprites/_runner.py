"""Remote process supervisor, executed inside the Sprite."""

COMMAND_RUNNER = r"""
import json
import os
import signal
import subprocess
import sys
import tempfile

process = None
pending_signal = None


def stop(signum, frame):
    global pending_signal
    if process is None:
        pending_signal = signum
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os._exit(128 + signum)


for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(signum, stop)
payload = json.load(sys.stdin)
# File-backed shell input avoids both URL and execve argument-size limits.
# The child gets EOF on stdin, matching non-interactive command execution.
script = tempfile.TemporaryFile()
script.write(payload["command"].encode())
script.seek(0)
process = subprocess.Popen(
    ["sh", "/dev/fd/" + str(script.fileno())],
    stdin=subprocess.DEVNULL,
    pass_fds=(script.fileno(),),
    env={**os.environ, **payload["env"]},
    cwd=payload["cwd"],
    start_new_session=True,
)
if pending_signal is not None:
    stop(pending_signal, None)
try:
    deadline = payload["timeout"]
    code = process.wait(timeout=deadline)
except subprocess.TimeoutExpired:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    sys.stderr.write(payload["marker"])
    sys.stderr.flush()
    sys.exit(124)
sys.exit(code if code >= 0 else 128 - code)
"""
