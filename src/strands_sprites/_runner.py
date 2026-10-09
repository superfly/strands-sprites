"""Remote process supervisor, executed inside the Sprite."""

COMMAND_RUNNER = r"""
import os
import signal
import subprocess
import sys

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
process = subprocess.Popen(sys.argv[3:], start_new_session=True)
if pending_signal is not None:
    stop(pending_signal, None)
try:
    deadline = None if sys.argv[1] == "None" else float(sys.argv[1])
    code = process.wait(timeout=deadline)
except subprocess.TimeoutExpired:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    sys.stderr.write(sys.argv[2])
    sys.stderr.flush()
    sys.exit(124)
sys.exit(code if code >= 0 else 128 - code)
"""
