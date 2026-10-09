"""Exercise the remote supervisor using real local POSIX processes."""

import json
import os
import shlex
import signal
import subprocess
import sys

import pytest

from strands_sprites._runner import COMMAND_RUNNER


@pytest.mark.skipif(sys.platform == "win32", reason="The remote supervisor requires POSIX process groups")
@pytest.mark.parametrize(
    "deadline, command, expected, marker_present",
    [
        ("None", "exit 124", 124, False),
        ("0.1", "trap '' TERM; sleep 5", 124, True),
    ],
)
def test_remote_supervisor_deadline(deadline, command, expected, marker_present):
    result = subprocess.run(
        [sys.executable, "-c", COMMAND_RUNNER],
        input=json.dumps(
            {
                "command": command,
                "timeout": None if deadline == "None" else float(deadline),
                "marker": "deadline-marker",
                "env": {},
                "cwd": None,
            }
        ).encode(),
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == expected
    assert (b"deadline-marker" in result.stderr) is marker_present


@pytest.mark.skipif(sys.platform == "win32", reason="The remote supervisor requires POSIX process groups")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)])
def test_disconnect_terminates_descendants(tmp_path, signum):
    side_effect = tmp_path / "side-effect"
    child = "import pathlib,time; print('ready',flush=True); time.sleep(0.5); pathlib.Path('side-effect').touch()"
    process = subprocess.Popen(
        [sys.executable, "-c", COMMAND_RUNNER],
        stdin=subprocess.PIPE,
        cwd=tmp_path,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process.stdin.write(
        json.dumps(
            {
                "command": shlex.join([sys.executable, "-c", child]),
                "timeout": 10,
                "marker": "deadline-marker",
                "env": {},
                "cwd": str(tmp_path),
            }
        ).encode()
    )
    process.stdin.close()
    process.stdin = None
    try:
        assert process.stdout.readline() == b"ready\n"
        os.killpg(process.pid, signum)
        process.communicate(timeout=3)
        assert process.returncode in (128 + signum, -signum)
        assert not side_effect.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
