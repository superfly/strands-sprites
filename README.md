# strands-sprites

A standalone [Strands](https://strandsagents.com/) sandbox extension for
[Sprites](https://sprites.dev/). Run an agent's shell commands and file operations
inside an existing Sprite, with files that persist between commands and agent runs.

## Install from source

This package is not yet published to PyPI. It requires Python 3.10 or later,
`strands-agents>=1.59.0,<2`, and `sprites-py>=0.7.2,<1`.

```sh
git clone https://github.com/superfly/strands-sprites.git
cd strands-sprites
python -m venv .venv
. .venv/bin/activate
python -m pip install .
```

The Sprite must already exist and have `python3` and `sh` on PATH. Choose an
existing absolute working directory, such as `/home/sprite`. If the Sprite uses
pyenv, `.python-version` and `PYENV_VERSION` must select an installed interpreter:
the process supervisor needs Python even for shell and file operations.

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

The sandbox registers `sprite_shell` and `sprite_file_editor` automatically.
Their names distinguish them from tools that execute locally. File and code
operations use Strands' `PosixShellSandbox` implementations.

A runnable version is in [examples/agent.py](examples/agent.py).

## Check the workspace without a model

With the same Sprite environment variables set:

```sh
python examples/workspace_check.py
# Or choose the Sprite and directory explicitly:
python examples/workspace_check.py --sprite my-sprite --working-dir /home/sprite
```

This check writes a uniquely named temporary file, reads it in a separate command,
and removes it. It needs only Sprites credentials; it does not call a model.

## Execution and lifecycle

- You own the `AsyncSpritesClient` and `AsyncSprite`. The adapter never creates or
  deletes a Sprite or closes the client. Keep the client open for the agent's lifetime.
- Files persist in the Sprite. Each command starts a fresh shell, so shell variables
  and `cd` do not persist. Use `working_dir`, or the per-command `cwd` and `env` options.
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
  status, and are logged locally with the SDK traceback. There are no automatic retries.
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
python -m mypy src
python -m build
python -m twine check dist/*
```

If you use Hatch, `hatch run prepare` runs formatting checks, lint, type checking,
and tests. Tests use mocks and local POSIX subprocesses; they do not require cloud
credentials. CI tests Python 3.10–3.14 and the minimum supported SDK versions. CI
builds distributions for validation only; there is no publishing workflow.

## License

Apache-2.0. See [LICENSE](LICENSE).
