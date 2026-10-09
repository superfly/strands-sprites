# strands-sprites

A standalone [Strands](https://strandsagents.com/) sandbox extension for
[Sprites](https://sprites.dev/). Run an agent's shell commands and file operations
inside an existing Sprite, with files that persist between commands and agent runs.

## Install

Requires Python 3.10 or later, `strands-agents>=1.59.0,<2`, and
`sprites-py>=0.7.2,<1`.

```sh
python -m pip install strands-sprites
```

To install from source instead:

```sh
git clone https://github.com/superfly/strands-sprites.git
cd strands-sprites
python -m venv .venv
. .venv/bin/activate
python -m pip install .
```

The Sprite must already exist. Shell and code execution require `python3` and
`sh` on the Sprite's default PATH, plus its standard `/dev/fd` filesystem.
Choose an existing absolute working directory, such as `/home/sprite`.
The supervisor starts in the Sprite's default directory before selecting the
command's directory and environment. Its Python must work there; a project's
pyenv settings can still affect interpreters launched by the command itself.
File operations use the filesystem API and do not require Python or a shell.
Each file write is limited to 16 MiB (16,777,216 bytes); larger writes are rejected
before making a network request.

Use an `AsyncSpritesClient` with `control_mode=False` (the default). The extension
rejects control-mode clients because supported SDK releases can drop streamed
output in that mode.

## Use with an agent

Set `SPRITES_TOKEN` and `SPRITE_NAME` in your environment, and configure the
credentials for your Strands model provider. This example uses Strands' default
model provider; you can pass your configured model as `Agent(model=..., sandbox=sandbox)`.

```python
import asyncio
import os

from sprites import AsyncSpritesClient
from strands import Agent
from strands_sprites import SpritesSandbox


async def main():
    async with AsyncSpritesClient(token=os.environ["SPRITES_TOKEN"]) as client:
        sandbox = SpritesSandbox(
            client.sprite(os.environ["SPRITE_NAME"]),
            working_dir="/home/sprite",
        )
        agent = Agent(sandbox=sandbox)
        await agent.invoke_async("List the files in the Sprite's working directory.")


asyncio.run(main())
```

The sandbox registers Strands' standard `sandbox_shell` and `sandbox_file_editor`
tools automatically, using the SDK descriptions with the Sprite's name appended.
File operations use the Sprites filesystem API, including directory metadata.
Code execution uses Strands' `PosixShellSandbox` interpreter command generation.

If you used the initial prototype, update any tool-name references from
`sprite_shell`/`sprite_file_editor` to `sandbox_shell`/`sandbox_file_editor`.

A runnable version is in [examples/agent.py](examples/agent.py).

## Check the workspace without a model

With the same Sprite environment variables set:

```sh
python examples/workspace_check.py
# Or choose the Sprite and directory explicitly:
python examples/workspace_check.py --sprite my-sprite --working-dir /home/sprite
```

This check uses uniquely named temporary files to verify persistence, a 1 MiB
filesystem round-trip, environment inheritance, and missing parent directory
creation. It also verifies that API-written files belong to the shell user and
can be changed through `sandbox_shell`, and that editing a 0755 script through
`sandbox_file_editor` preserves its mode. It removes its temporary files and
directories afterward. It needs only Sprites credentials; it does not call a model.

## Execution and lifecycle

- You own the `AsyncSpritesClient` and `AsyncSprite`. The adapter never creates or
  deletes a Sprite or closes the client. Keep the client open for the agent's lifetime.
- Files persist in the Sprite. Each command starts a fresh shell, so shell variables
  and `cd` do not persist. Use `working_dir`, or the per-command `cwd` and `env` options.
  The supervisor explicitly merges `env` over its inherited environment.
- Commands, code, working directories and environment values travel as a JSON
  payload over WebSocket stdin. They are not placed in the exec URL. The supervisor
  runs commands from an unnamed temporary script, avoiding OS argument-size limits.
  File writes send raw bytes in an HTTP body, avoiding base64 command-size limits.
- `working_dir` selects a directory; it does not restrict filesystem access.
  Commands run with the Sprite user's permissions.
- Shell output streams asynchronously. The final result preserves whitespace and
  includes the complete stdout and stderr. Output is buffered without a size limit;
  redirect large output to files when needed.
- A positive, finite `timeout` sets a remote command deadline. The supervisor kills
  the command process group at that deadline. A confirmed deadline raises Strands'
  `SandboxTimeoutError` with partial output. An ordinary `exit 124` remains an exit code.
  `timeout=None` means there is no command deadline.
- The SDK transport gets an additional 30 seconds beyond a finite command deadline.
  Transport failures raise a sanitized `RuntimeError` reporting unknown execution
  status. HTTP 401, 403, 404 and 429 responses instead identify authentication,
  authorization, missing resources and rate limits. SDK failures are logged locally
  with their traceback. There are no automatic retries.
- Cancellation closes the transport. Remote cleanup follows the platform's disconnect
  grace period, and the supervisor forwards termination to the command process group.
  Cancellation is not an immediate acknowledgement that remote execution has stopped.
  Processes that deliberately detach from that group can outlive it.

The extension supports persistent shell and file access. It does not currently
expose checkpoint or Sprite lifecycle management as agent tools.

## Development

```sh
python -m pip install -e '.[dev]'
python -m pytest --cov=strands_sprites
python -m ruff check .
python -m ruff format --check .
python -m mypy src scripts
python -m build
python -m twine check dist/*
```

If you use Hatch, `hatch run prepare` runs formatting checks, lint, type checking,
and tests. Tests use mocks and local POSIX subprocesses; they do not require cloud
credentials. CI tests Python 3.10–3.14 and resolves minimum direct dependencies
from scratch with `uv pip install --resolution lowest-direct`. Pull requests run
CI once; pushes run it only on `main`. A weekly job checks the latest published
Strands and Sprites SDKs for compatibility.

## Releases

See [RELEASING.md](https://github.com/superfly/strands-sprites/blob/main/RELEASING.md) for the maintainer release process.

## License

Apache-2.0. See [LICENSE](LICENSE).
