"""Tests for the Sprites sandbox without a cloud account."""

import asyncio
import json
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from sprites import AsyncSpritesClient
from sprites.exceptions import APIError, ExitError, NetworkError
from sprites.exceptions import TimeoutError as SpriteTimeoutError
from sprites.websocket import WSCommand
from strands import Agent, SandboxTimeoutError
from strands.models import Model
from strands.sandbox import ExecutionResult, StreamChunk
from strands.sandbox.errors import SandboxPathNotFoundError

from strands_sprites import SpritesSandbox


def mock_sprite():
    sprite = MagicMock()
    sprite.client.control_mode = False
    sprite.name = "test-sprite"
    return sprite


@pytest.mark.asyncio
async def test_streams_before_completion_and_decodes_split_utf8():
    sprite = mock_sprite()
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
    sprite = mock_sprite()

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
    sprite = mock_sprite()

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
    sprite = mock_sprite()
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
    sprite = mock_sprite()
    with pytest.raises(ValueError):
        await SpritesSandbox(sprite, working_dir="/workspace").execute("command", timeout=timeout)
    sprite.command.assert_not_called()


@pytest.mark.asyncio
async def test_streams_stderr_without_waiting_for_command_completion():
    sprite = mock_sprite()
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
    sprite = mock_sprite()

    async def run():
        arguments = sprite.command.call_args
        marker = json.loads(arguments.kwargs["stdin"].getvalue())["marker"].encode()
        arguments.kwargs["stderr"].write(b"partial error" + marker[:10])
        arguments.kwargs["stderr"].write(marker[10:])
        raise ExitError("exit status 124", 124, b"", b"")

    sprite.command.return_value.run = run
    with pytest.raises(SandboxTimeoutError) as error:
        await SpritesSandbox(sprite, working_dir="/workspace").execute("command", timeout=1)
    assert error.value.stderr == "partial error"


@pytest.mark.asyncio
async def test_forwards_cwd_environment_and_separate_transport_deadline():
    sprite = mock_sprite()

    async def run():
        pass

    sprite.command.return_value.run = run
    await SpritesSandbox(sprite, working_dir="/workspace").execute(
        "echo '$VALUE'", timeout=2.5, cwd="/other path", env={"VALUE": "a; b"}
    )
    payload = json.loads(sprite.command.call_args.kwargs["stdin"].getvalue())
    assert payload["command"] == "echo '$VALUE'"
    assert payload["cwd"] == "/other path"
    assert payload["env"] == {"VALUE": "a; b"}
    assert "env" not in sprite.command.call_args.kwargs
    assert "cwd" not in sprite.command.call_args.kwargs
    assert sprite.command.call_args.kwargs["timeout"] == 32.5


def test_agent_registers_remote_tools_without_host_name_collision():
    sandbox = SpritesSandbox(mock_sprite(), working_dir="/workspace")
    agent = Agent(model=MagicMock(spec=Model), sandbox=sandbox)
    assert set(agent.tool_names) == {"sandbox_shell", "sandbox_file_editor"}


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="The Sprite runtime uses POSIX shell commands")
async def test_real_sdk_binary_file_roundtrip_over_local_transport(monkeypatch, tmp_path):
    async def local_transport(command):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *command.args[1:],
            cwd=command.dir,
            env=os.environ.copy(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert command.env == {}
        assert len(WSCommand(command)._build_websocket_url()) < 8192
        stdout, stderr = await process.communicate(command.stdin.read())
        command.stdout.write(stdout)
        command.stderr.write(stderr)
        return process.returncode

    monkeypatch.setattr("sprites.websocket.run_ws_command", local_transport)
    async with AsyncSpritesClient(token="test-token") as client:
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(local_filesystem(tmp_path)))
        sandbox = SpritesSandbox(client.sprite("test-sprite"), working_dir=str(tmp_path))
        filename = str(tmp_path / "quoted ' file.bin")
        content = bytes(range(256)) * 4096
        await sandbox.write_file(filename, content)
        assert await sandbox.read_file(filename) == content
        result = await sandbox.execute("printf '%s' \"$VALUE\"", env={"VALUE": "persistent workspace"})
        assert result == ExecutionResult(exit_code=0, stdout="persistent workspace", stderr="")
        result = await sandbox.execute_code("#" + "x" * 1_000_000 + "\nprint('large script')", "python3", timeout=5)
        assert result.stdout == "large script\n"
        result = await sandbox.execute('printf "%s:%s" "$X" "$PATH"', env={"X": "1"})
        assert result.stdout.startswith("1:") and len(result.stdout) > 2
        await sandbox.remove_file(filename)
        assert not (tmp_path / "quoted ' file.bin").exists()
        example = runpy.run_path(str(Path(__file__).parents[1] / "examples" / "workspace_check.py"))
        await example["check_workspace"](sandbox)
        assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "1INVALID", "KEY=VALUE", "KEY;cmd"])
async def test_invalid_environment_names_do_not_start_command(key):
    sprite = mock_sprite()
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        await SpritesSandbox(sprite, working_dir="/workspace").execute("command", env={key: "value"})
    sprite.command.assert_not_called()


@pytest.mark.parametrize("path", ["relative", "/contains\0nul"])
def test_invalid_working_directory(path):
    with pytest.raises(ValueError, match="absolute POSIX path"):
        SpritesSandbox(mock_sprite(), working_dir=path)


def local_filesystem(root):
    def handle(request):
        path = Path(request.url.params["path"])
        assert path.is_relative_to(root)
        assert len(str(request.url)) < 2048
        if request.url.path.endswith("/write"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(request.content)
            return httpx.Response(200, json={})
        if not path.exists():
            return httpx.Response(404, json={"code": "ENOENT"})
        if request.url.path.endswith("/read"):
            if path.is_dir():
                return httpx.Response(400, json={"code": "EISDIR", "error": "is a directory"})
            return httpx.Response(200, content=path.read_bytes())
        if request.url.path.endswith("/delete"):
            path.unlink()
            return httpx.Response(200, json={})
        if request.url.path.endswith("/list"):
            entries = sorted(path.iterdir()) if path.is_dir() else [path]
            return httpx.Response(
                200,
                json={
                    "path": str(path),
                    "entries": [
                        {"name": p.name, "path": str(p), "isDir": p.is_dir(), "size": p.stat().st_size} for p in entries
                    ],
                },
            )
        raise AssertionError(request.url)

    return handle


@pytest.mark.asyncio
async def test_control_mode_rejected_before_io():
    async with AsyncSpritesClient(token="test-token", control_mode=True) as client:
        with pytest.raises(ValueError, match="control_mode"):
            SpritesSandbox(client.sprite("test-sprite"), working_dir="/workspace")


@pytest.mark.asyncio
async def test_control_mode_change_rejected_before_command():
    sprite = mock_sprite()
    sandbox = SpritesSandbox(sprite, working_dir="/workspace")
    sprite.client.control_mode = True
    with pytest.raises(ValueError, match="control_mode"):
        await sandbox.execute("command")
    sprite.command.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, category", [(401, "authentication"), (403, "authorization"), (404, "not found"), (429, "rate limit")]
)
async def test_api_error_category_is_safe(status, category):
    sprite = mock_sprite()

    async def run():
        raise APIError("private-command?env=secret", status_code=status)

    sprite.command.return_value.run = run
    with pytest.raises(RuntimeError, match=category) as error:
        await SpritesSandbox(sprite, working_dir="/workspace").execute("command")
    assert "secret" not in str(error.value)
    assert "unknown" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_native_filesystem_metadata_and_editor_preserve_contents(tmp_path):
    from strands.vended_tools.file_editor.file_editor import _handle_insert

    (tmp_path / "empty").mkdir()
    (tmp_path / "nonempty").mkdir()
    (tmp_path / "nonempty" / "child").write_bytes(b"child")
    (tmp_path / "notes.txt").write_bytes(b"before\nafter\n")
    async with AsyncSpritesClient(token="test-token") as client:
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(local_filesystem(tmp_path)))
        sandbox = SpritesSandbox(client.sprite("test-sprite"), working_dir=str(tmp_path))
        sandbox.sprite.command = MagicMock(side_effect=AssertionError("File operations must not exec"))
        entries = {entry.name: entry for entry in await sandbox.list_files(".")}
        assert entries["empty"].is_dir and entries["nonempty"].is_dir
        assert entries["notes.txt"].is_dir is False
        assert entries["notes.txt"].size == len(b"before\nafter\n")
        assert await sandbox.list_files("empty") == []
        for path in ("missing", "notes.txt"):
            with pytest.raises(SandboxPathNotFoundError):
                await sandbox.list_files(path)
        with pytest.raises(FileNotFoundError):
            await sandbox.read_file("missing")
        with pytest.raises(IsADirectoryError):
            await sandbox.read_file("empty")
        await _handle_insert(sandbox, str(tmp_path / "notes.txt"), 1, "inserted")
        assert await sandbox.read_file("notes.txt") == b"before\ninserted\nafter\n"
        await sandbox.write_file("nested/file.bin", bytes(range(256)) * 4096)
        assert len(await sandbox.read_file("nested/file.bin")) == 1_048_576
        await sandbox.remove_file("nested/file.bin")
        with pytest.raises(FileNotFoundError):
            await sandbox.remove_file("nested/file.bin")


def test_descriptions_match_strands_and_name_the_sprite():
    from strands.vended_tools.file_editor.file_editor import DEFAULT_FILE_EDITOR_DESCRIPTION
    from strands.vended_tools.shell.types import SANDBOX_SHELL_DESCRIPTION

    tools = SpritesSandbox(mock_sprite(), working_dir="/workspace").get_tools()
    descriptions = {tool.tool_name: tool.tool_spec["description"] for tool in tools}
    assert descriptions["sandbox_file_editor"].startswith(DEFAULT_FILE_EDITOR_DESCRIPTION)
    assert descriptions["sandbox_shell"].startswith(SANDBOX_SHELL_DESCRIPTION)
    assert all("test-sprite" in description for description in descriptions.values())


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="The remote supervisor requires POSIX")
async def test_large_code_and_secret_environment_through_sdk_websocket(monkeypatch, tmp_path):
    from urllib.parse import parse_qs, urlsplit

    class ExecSocket:
        def __init__(self, process):
            self.process = process
            self.messages = asyncio.Queue()
            self.close_code = 1000
            self.close_reason = ""
            self.pump = asyncio.create_task(self.collect())

        async def collect(self):
            async def copy(reader, stream):
                while data := await reader.read(4096):
                    await self.messages.put(bytes([stream]) + data)

            await asyncio.gather(copy(self.process.stdout, 1), copy(self.process.stderr, 2))
            code = await self.process.wait()
            await self.messages.put(bytes([3, code]))
            await self.messages.put(None)

        async def send(self, data):
            if data[0] == 0:
                self.process.stdin.write(data[1:])
                await self.process.stdin.drain()
            elif data[0] == 4:
                self.process.stdin.close()
            else:
                raise AssertionError("Unexpected stdin frame")

        def __aiter__(self):
            return self

        async def __anext__(self):
            message = await self.messages.get()
            if message is None:
                raise StopAsyncIteration
            return message

        async def close(self):
            await self.pump

    urls = []

    async def connect(url, **kwargs):
        urls.append(url)
        query = parse_qs(urlsplit(url).query)
        assert "env" not in query
        assert "secret-value" not in url
        assert len(url) < 8192
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *query["cmd"][1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return ExecSocket(process)

    monkeypatch.setattr("sprites.websocket.connect", connect)
    async with AsyncSpritesClient(token="test-token") as client:
        sandbox = SpritesSandbox(client.sprite("test-sprite"), working_dir=str(tmp_path))
        result = await sandbox.execute_code(
            "#" + "x" * 1_000_000 + "\nimport os; print(os.environ['SECRET']); print(bool(os.environ.get('PATH')))",
            "python3",
            env={"SECRET": "secret-value"},
            timeout=5,
        )
        assert result.stdout == "secret-value\nTrue\n"
        with pytest.raises(SandboxTimeoutError):
            await sandbox.execute("sleep 5", timeout=0.1)
    assert len(urls) == 2


@pytest.mark.asyncio
async def test_oversized_write_is_rejected_before_io():
    sprite = mock_sprite()
    sandbox = SpritesSandbox(sprite, working_dir="/workspace")
    with pytest.raises(ValueError, match="16 MiB"):
        await sandbox.write_file("large.bin", b"x" * (16 * 1024 * 1024 + 1))
    sprite.filesystem.assert_not_called()


@pytest.mark.asyncio
async def test_write_at_limit_is_allowed():
    from unittest.mock import AsyncMock

    sprite = mock_sprite()
    write = AsyncMock()
    sprite.filesystem.return_value.path.return_value.write_bytes = write
    content = b"x" * (16 * 1024 * 1024)
    await SpritesSandbox(sprite, working_dir="/workspace").write_file("large.bin", content)
    write.assert_awaited_once_with(content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, code, operation, expected, message",
    [
        (400, "ENOTDIR", "read_file", NotADirectoryError, "not a directory"),
        (400, "ENOTDIR", "list_files", SandboxPathNotFoundError, "target"),
        (403, "EACCES", "write_file", PermissionError, "permission denied"),
        (500, None, "read_file", OSError, "connectivity"),
    ],
)
async def test_filesystem_errors_are_actionable_and_sanitized(caplog, status, code, operation, expected, message):
    def handle(request):
        return httpx.Response(status, json={"code": code, "error": "private-token-and-url"})

    async with AsyncSpritesClient(token="test-token") as client:
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        sandbox = SpritesSandbox(client.sprite("test-sprite"), working_dir="/workspace")
        args = ("target", b"content") if operation == "write_file" else ("target",)
        with pytest.raises(expected, match=message) as error:
            await getattr(sandbox, operation)(*args)
        assert "private-token-and-url" not in str(error.value)
        assert error.value.__suppress_context__
        if status == 500:
            assert "private-token-and-url" in caplog.text
