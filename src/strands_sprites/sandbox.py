"""Sprites sandbox with persistent files and native asynchronous streaming.

Install the ``strands-sprites`` extension. The caller owns the AsyncSprite and its
client; this adapter never creates or deletes Sprites or closes that client.
Shell execution needs working ``python3`` and ``sh`` on the default PATH.
File operations use the filesystem API. Working directories are not
filesystem security boundaries.
"""

import asyncio
import codecs
import io
import json
import logging
import math
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import Any

from sprites import AsyncCmd, AsyncSprite
from sprites.exceptions import (
    APIError,
    AuthenticationError,
    ExitError,
    FileNotFoundError_,
    IsADirectoryError_,
    NotADirectoryError_,
    NotFoundError,
    PermissionError_,
    SpriteError,
)
from strands import SandboxTimeoutError
from strands.sandbox import ExecutionResult, FileInfo, PosixShellSandbox, StreamChunk, StreamType
from strands.sandbox.errors import SandboxPathNotFoundError
from strands.sandbox.posix_shell import validate_env_keys
from strands.types.tools import AgentTool
from strands.vended_tools.file_editor import make_file_editor
from strands.vended_tools.file_editor.file_editor import DEFAULT_FILE_EDITOR_DESCRIPTION
from strands.vended_tools.shell import make_shell
from strands.vended_tools.shell.types import SANDBOX_SHELL_DESCRIPTION

from ._runner import COMMAND_RUNNER

logger = logging.getLogger(__name__)


class _OutputSink:
    """Decode streaming bytes and remove the private remote deadline marker."""

    def __init__(self, queue: asyncio.Queue[StreamChunk | None], stream: StreamType, marker: bytes = b"") -> None:
        self.queue = queue
        self.stream = stream
        self.marker = marker
        self.pending = b""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.parts: list[str] = []
        self.timed_out = False

    def write(self, data: bytes) -> int:
        self.pending += data
        if self.marker and self.marker in self.pending:
            self.pending = self.pending.replace(self.marker, b"")
            self.timed_out = True
        keep = min(len(self.pending), max(0, len(self.marker) - 1))
        while keep and not self.marker.startswith(self.pending[-keep:]):
            keep -= 1
        count = max(0, len(self.pending) - keep)
        self._emit(self.pending[:count])
        self.pending = self.pending[count:]
        return len(data)

    def _emit(self, data: bytes, *, final: bool = False) -> None:
        text = self.decoder.decode(data, final=final)
        if text:
            self.parts.append(text)
            self.queue.put_nowait(StreamChunk(data=text, stream_type=self.stream))

    def finish(self) -> None:
        self._emit(self.pending, final=True)
        self.pending = b""

    def text(self) -> str:
        return "".join(self.parts)


def _request_failure(error: Exception) -> str:
    status = error.status_code if isinstance(error, APIError) else None
    if isinstance(error, AuthenticationError) or status == 401:
        return "Sprite request rejected: authentication failed (HTTP 401)."
    if status == 403:
        return "Sprite request rejected: authorization failed (HTTP 403)."
    if isinstance(error, NotFoundError) or status == 404:
        return "Sprite request rejected: resource not found (HTTP 404)."
    if status == 429:
        return "Sprite request rejected: rate limit exceeded (HTTP 429)."
    return "Sprite request failed; execution status is unknown. Check the Sprite before retrying."


@contextmanager
def _filesystem_errors(path: str, *, listing: bool = False) -> Iterator[None]:
    try:
        yield
    except FileNotFoundError_:
        if listing:
            raise SandboxPathNotFoundError(path) from None
        raise FileNotFoundError(f"Sprite file not found: {path}") from None
    except NotADirectoryError_:
        if listing:
            raise SandboxPathNotFoundError(path) from None
        raise NotADirectoryError(f"Sprite path is not a directory: {path}") from None
    except IsADirectoryError_:
        raise IsADirectoryError(f"Sprite path is a directory: {path}") from None
    except PermissionError_:
        raise PermissionError(f"Sprite filesystem permission denied: {path}") from None
    except SpriteError:
        logger.warning("Sprite filesystem request failed", exc_info=True)
        raise OSError("Sprite filesystem request failed; check credentials, permissions and connectivity.") from None


async def _run(command: AsyncCmd, stdout: _OutputSink, stderr: _OutputSink) -> int:
    try:
        await command.run()
        return 0
    except ExitError as error:
        return error.exit_code()
    except (SpriteError, TimeoutError) as error:
        logger.warning("error_type=<%s> | sprite execution failed", type(error).__name__, exc_info=True)
        raise RuntimeError(_request_failure(error)) from None
    finally:
        stdout.finish()
        stderr.finish()
        stdout.queue.put_nowait(None)


class SpritesSandbox(PosixShellSandbox):
    """Run shell and file tools in a caller-owned, persistent Sprite.

    File operations use the filesystem API. Commands and code travel over stdin.
    Each command starts a fresh process. A remote supervisor kills the command
    process group at its deadline and forwards termination signals on disconnect.
    Cancellation closes the transport; remote cleanup follows the platform's
    disconnect grace period, so it is not an immediate termination acknowledgement.

    See the README and examples/agent.py for complete asynchronous usage.
    """

    def __init__(self, sprite: AsyncSprite, *, working_dir: str) -> None:
        """Attach to an existing Sprite without making a network request.

        Args:
            sprite: Caller-owned asynchronous Sprite handle.
            working_dir: Existing absolute directory inside the Sprite.

        Raises:
            ValueError: If the working directory is not absolute or control mode is enabled.
        """
        if not working_dir.startswith("/") or "\0" in working_dir:
            raise ValueError("working_dir must be an absolute POSIX path")
        self.sprite = sprite
        self._check_control_mode()
        self.working_dir = working_dir

    def _check_control_mode(self) -> None:
        if self.sprite.client.control_mode:
            raise ValueError("SpritesSandbox requires a client with control_mode=False for reliable output streaming")

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        """Read bytes through the filesystem API without executing a command."""
        with _filesystem_errors(path):
            return await self.sprite.filesystem(self.working_dir).path(path).read_bytes()

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        """Write a binary HTTP body, creating missing parent directories."""
        with _filesystem_errors(path):
            await self.sprite.filesystem(self.working_dir).path(path).write_bytes(content)

    async def remove_file(self, path: str, **kwargs: Any) -> None:
        """Remove a file through the filesystem API."""
        with _filesystem_errors(path):
            await self.sprite.filesystem(self.working_dir).path(path).unlink()

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        """List native directory entries, preserving directory flags and sizes."""
        remote = self.sprite.filesystem(self.working_dir).path(path)
        with _filesystem_errors(path, listing=True):
            # SDK 0.7.2 iterdir() discards metadata, and stat() on a directory
            # reports its first child. Keep the native list response instead.
            response = await remote._request(
                "GET", "/list", "list", params={"path": str(remote), "workingDir": self.working_dir}
            )
            entries = response.json().get("entries") or []
            if any(entry.get("path") == str(remote) and not entry.get("isDir") for entry in entries):
                raise SandboxPathNotFoundError(path)
            return [
                FileInfo(name=entry["name"], is_dir=entry.get("isDir"), size=entry.get("size")) for entry in entries
            ]

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Execute a command in the Sprite, streaming output as it arrives.

        Args:
            command: POSIX shell command.
            timeout: Positive, finite remote deadline in seconds; None means no deadline.
            cwd: Directory overriding working_dir for this command.
            env: Environment variables applied only to this command.
            **kwargs: Additional arguments for interface compatibility.

        Yields:
            StreamChunk objects followed by one ExecutionResult, preserving whitespace.

        Raises:
            ValueError: If timeout or environment variable names are invalid.
            SandboxTimeoutError: If the remote supervisor confirms the deadline elapsed.
            RuntimeError: If the transport fails and execution status is unknown.
        """
        self._check_control_mode()
        if timeout is not None and (isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be positive and finite, or None")
        if env:
            validate_env_keys(env)
        queue: asyncio.Queue[StreamChunk | None] = asyncio.Queue()
        marker = f"\n__sprites_timeout_{uuid.uuid4().hex}__\n"
        stdout = _OutputSink(queue, "stdout")
        stderr = _OutputSink(queue, "stderr", marker.encode())
        payload = json.dumps(
            {
                "command": command,
                "env": env or {},
                "cwd": cwd if cwd is not None else self.working_dir,
                "timeout": timeout,
                "marker": marker,
            }
        ).encode()
        remote = self.sprite.command(
            "python3",
            "-c",
            COMMAND_RUNNER,
            stdin=io.BytesIO(payload),
            timeout=timeout + 30 if timeout is not None else None,
            stdout=stdout,
            stderr=stderr,
        )
        task = asyncio.create_task(_run(remote, stdout, stderr))
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
            code = await task
            if stderr.timed_out:
                raise SandboxTimeoutError(timeout, stdout.text(), stderr.text())
            yield ExecutionResult(exit_code=code, stdout=stdout.text(), stderr=stderr.text())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def get_tools(self) -> list[AgentTool]:
        """Return the standard Strands sandbox tools with their Sprite identified."""
        return [
            make_file_editor(
                sandbox=self,
                name="sandbox_file_editor",
                description=f'{DEFAULT_FILE_EDITOR_DESCRIPTION} Files are in Sprite "{self.sprite.name}".',
            ),
            make_shell(
                sandbox=self,
                name="sandbox_shell",
                description=(
                    f'{SANDBOX_SHELL_DESCRIPTION} Runs in Sprite "{self.sprite.name}". '
                    f"Working directory: {self.working_dir}."
                ),
            ),
        ]
