"""Sprites sandbox with persistent files and native asynchronous streaming.

Install the ``strands-sprites`` extension. The caller owns the AsyncSprite and its
client; this adapter never creates or deletes Sprites or closes that client.
The Sprite must have a working ``python3`` and ``sh`` on PATH, including when
workspace pyenv settings select an interpreter. Working directories are not
filesystem security boundaries.
"""

import asyncio
import codecs
import logging
import math
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from sprites import AsyncCmd, AsyncSprite
from sprites.exceptions import ExitError, SpriteError
from strands import SandboxTimeoutError
from strands.sandbox import ExecutionResult, PosixShellSandbox, StreamChunk, StreamType
from strands.types.tools import AgentTool
from strands.vended_tools.file_editor import make_file_editor
from strands.vended_tools.shell import make_shell

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


async def _run(command: AsyncCmd, stdout: _OutputSink, stderr: _OutputSink) -> int:
    try:
        await command.run()
        return 0
    except ExitError as error:
        return error.exit_code()
    except (SpriteError, TimeoutError) as error:
        logger.warning("error_type=<%s> | sprite execution failed", type(error).__name__, exc_info=True)
        raise RuntimeError(
            "Sprite request failed; execution status is unknown. Check the Sprite before retrying."
        ) from None
    finally:
        stdout.finish()
        stderr.finish()
        stdout.queue.put_nowait(None)


class SpritesSandbox(PosixShellSandbox):
    """Run shell and file tools in a caller-owned, persistent Sprite.

    File and code operations use the base class's POSIX shell implementations.
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
            ValueError: If the working directory is not absolute.
        """
        if not working_dir.startswith("/") or "\0" in working_dir:
            raise ValueError("working_dir must be an absolute POSIX path")
        self.sprite = sprite
        self.working_dir = working_dir

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
        if timeout is not None and (isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be positive and finite, or None")
        if env:
            for key in env:
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
                    raise ValueError(f"Invalid environment variable name: {key}")
        queue: asyncio.Queue[StreamChunk | None] = asyncio.Queue()
        marker = f"\n__sprites_timeout_{uuid.uuid4().hex}__\n"
        stdout = _OutputSink(queue, "stdout")
        stderr = _OutputSink(queue, "stderr", marker.encode())
        remote = self.sprite.command(
            "python3",
            "-c",
            COMMAND_RUNNER,
            str(timeout),
            marker,
            "sh",
            "-c",
            command,
            cwd=cwd if cwd is not None else self.working_dir,
            env=env,
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
        """Return shell and file tools whose names explicitly identify the Sprite."""
        return [
            make_file_editor(
                sandbox=self,
                name="sprite_file_editor",
                description=(
                    "View, create, and edit files inside the persistent Sprite. "
                    "Supports view, create, str_replace, and insert. Use absolute paths."
                ),
            ),
            make_shell(
                sandbox=self,
                name="sprite_shell",
                description=(
                    "Run a shell command inside the persistent Sprite and return stdout, stderr, and exit code. "
                    "Each call starts a fresh shell. "
                    f"Working directory: {self.working_dir}."
                ),
            ),
        ]
