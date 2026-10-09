"""Tests for the Sprites sandbox without a cloud account."""

import asyncio
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sprites import AsyncSpritesClient
from sprites.exceptions import ExitError, NetworkError
from sprites.exceptions import TimeoutError as SpriteTimeoutError
from strands import Agent, SandboxTimeoutError
from strands.models import Model
from strands.sandbox import ExecutionResult, StreamChunk

from strands_sprites import SpritesSandbox


@pytest.mark.asyncio
async def test_streams_before_completion_and_decodes_split_utf8():
    sprite = MagicMock()
    finished = asyncio.Event()

    async def run():
        sinks = sprite.command.call_args.kwargs
        sinks["stdout"].write(b"hello ")
        sinks["stdout"].write(b"\xe2\x82")
        sinks["stdout"].write(b"\xac")
        await finished.wait()

    sprite.command.return_value.run = run
    stream = SpritesSandbox(sprite, working_dir="/workspace").execute_streaming("command")
    first = await asyncio.wait_for(anext(stream), 1)
    assert first == StreamChunk(data="hello ", stream_type="stdout")
    finished.set()
    rest = [chunk async for chunk in stream]
    assert rest[-1] == ExecutionResult(exit_code=0, stdout="hello €", stderr="")


@pytest.mark.asyncio
async def test_nonzero_exit_preserves_whitespace_and_streams():
    sprite = MagicMock()

    async def run():
        sinks = sprite.command.call_args.kwargs
        sinks["stdout"].write(b"  out\n")
        sinks["stderr"].write(b"err\n")
        raise ExitError("exit status 124", 124, b"", b"")

    sprite.command.return_value.run = run
    result = await SpritesSandbox(sprite, working_dir="/workspace").execute("exit 124", timeout=1)
    assert result == ExecutionResult(exit_code=124, stdout="  out\n", stderr="err\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [NetworkError, SpriteTimeoutError, TimeoutError])
async def test_transport_error_is_logged_but_not_exposed(caplog, error_type):
    sprite = MagicMock()

    async def run():
        raise error_type("private diagnostic")

    sprite.command.return_value.run = run
    with pytest.raises(RuntimeError, match="execution status is unknown") as error:
        await SpritesSandbox(sprite, working_dir="/workspace").execute("command")
    assert error.value.__suppress_context__
    assert "private diagnostic" not in str(error.value)
    assert "private diagnostic" in caplog.text
    assert sprite.command.call_count == 1


@pytest.mark.asyncio
async def test_closing_stream_cancels_transport_without_closing_caller_client():
    sprite = MagicMock()
    cancelled = asyncio.Event()

    async def run():
        sprite.command.call_args.kwargs["stdout"].write(b"started")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sprite.command.return_value.run = run
    stream = SpritesSandbox(sprite, working_dir="/workspace").execute_streaming("command")
    await anext(stream)
    await stream.aclose()
    assert cancelled.is_set()
    sprite.delete.assert_not_called()
    sprite.client.close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
async def test_invalid_deadline_does_not_start_command(timeout):
    sprite = MagicMock()
    with pytest.raises(ValueError):
        await SpritesSandbox(sprite, working_dir="/workspace").execute("command", timeout=timeout)
    sprite.command.assert_not_called()


@pytest.mark.asyncio
async def test_streams_stderr_without_waiting_for_command_completion():
    sprite = MagicMock()
    finished = asyncio.Event()

    async def run():
        sprite.command.call_args.kwargs["stderr"].write(b"diagnostic")
        await finished.wait()

    sprite.command.return_value.run = run
    stream = SpritesSandbox(sprite, working_dir="/workspace").execute_streaming("command")
    first = await asyncio.wait_for(anext(stream), 1)
    assert first == StreamChunk(data="diagnostic", stream_type="stderr")
    finished.set()
    await stream.aclose()


@pytest.mark.asyncio
async def test_confirmed_timeout_marker_is_removed_even_when_split():
    sprite = MagicMock()

    async def run():
        arguments = sprite.command.call_args
        marker = arguments.args[4].encode()
        arguments.kwargs["stderr"].write(b"partial error" + marker[:10])
        arguments.kwargs["stderr"].write(marker[10:])
        raise ExitError("exit status 124", 124, b"", b"")

    sprite.command.return_value.run = run
    with pytest.raises(SandboxTimeoutError) as error:
        await SpritesSandbox(sprite, working_dir="/workspace").execute("command", timeout=1)
    assert error.value.stderr == "partial error"


@pytest.mark.asyncio
async def test_forwards_cwd_environment_and_separate_transport_deadline():
    sprite = MagicMock()

    async def run():
        pass

    sprite.command.return_value.run = run
    await SpritesSandbox(sprite, working_dir="/workspace").execute(
        "echo '$VALUE'", timeout=2.5, cwd="/other path", env={"VALUE": "a; b"}
    )
    assert sprite.command.call_args.args[-1] == "echo '$VALUE'"
    assert sprite.command.call_args.kwargs["cwd"] == "/other path"
    assert sprite.command.call_args.kwargs["env"] == {"VALUE": "a; b"}
    assert sprite.command.call_args.kwargs["timeout"] == 32.5


def test_agent_registers_remote_tools_without_host_name_collision():
    sandbox = SpritesSandbox(MagicMock(), working_dir="/workspace")
    agent = Agent(model=MagicMock(spec=Model), sandbox=sandbox)
    assert set(agent.tool_names) == {"sprite_shell", "sprite_file_editor"}


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="The Sprite runtime uses POSIX shell commands")
async def test_real_sdk_binary_file_roundtrip_over_local_transport(monkeypatch, tmp_path):
    async def local_transport(command):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *command.args[1:],
            cwd=command.dir,
            env={**os.environ, **command.env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        command.stdout.write(stdout)
        command.stderr.write(stderr)
        return process.returncode

    monkeypatch.setattr("sprites.websocket.run_ws_command", local_transport)
    async with AsyncSpritesClient(token="test-token") as client:
        sandbox = SpritesSandbox(client.sprite("test-sprite"), working_dir=str(tmp_path))
        filename = str(tmp_path / "quoted ' file.bin")
        content = bytes(range(256))
        await sandbox.write_file(filename, content)
        assert await sandbox.read_file(filename) == content
        result = await sandbox.execute("printf '%s' \"$VALUE\"", env={"VALUE": "persistent workspace"})
        assert result == ExecutionResult(exit_code=0, stdout="persistent workspace", stderr="")
        await sandbox.remove_file(filename)
        assert not (tmp_path / "quoted ' file.bin").exists()
        example = runpy.run_path(str(Path(__file__).parents[1] / "examples" / "workspace_check.py"))
        await example["check_workspace"](sandbox)
        assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "1INVALID", "KEY=VALUE", "KEY;cmd"])
async def test_invalid_environment_names_do_not_start_command(key):
    sprite = MagicMock()
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        await SpritesSandbox(sprite, working_dir="/workspace").execute("command", env={key: "value"})
    sprite.command.assert_not_called()


@pytest.mark.parametrize("path", ["relative", "/contains\0nul"])
def test_invalid_working_directory(path):
    with pytest.raises(ValueError, match="absolute POSIX path"):
        SpritesSandbox(MagicMock(), working_dir=path)
