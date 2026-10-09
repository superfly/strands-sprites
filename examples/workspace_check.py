"""Verify Sprite execution and filesystem contracts without a model call."""

import argparse
import asyncio
import os
import shlex
import uuid

from sprites import AsyncSpritesClient

from strands_sprites import SpritesSandbox


async def check_workspace(sandbox: SpritesSandbox) -> None:
    path = f"{sandbox.working_dir.rstrip('/')}/.strands-sprites-check-{uuid.uuid4().hex}"
    quoted = shlex.quote(path)
    directory = path + "-dir"
    nested = directory + "/new_dir/file.txt"
    script = directory + "/script.sh"
    tools = {tool.tool_name: tool for tool in sandbox.get_tools()}
    try:
        written = await sandbox.execute(f"printf '%s' 'persistent workspace' > {quoted}", timeout=30)
        if written.exit_code != 0:
            raise RuntimeError("Workspace check could not write its temporary file.")
        read = await sandbox.execute(f"cat {quoted}", timeout=30)
        if read.exit_code != 0 or read.stdout != "persistent workspace":
            raise RuntimeError("Workspace check could not read the first command's file.")
        content = bytes(range(256)) * 4096
        await sandbox.write_file(path, content)
        if await sandbox.read_file(path) != content:
            raise RuntimeError("Workspace check failed its 1 MiB filesystem round-trip.")
        environment = await sandbox.execute('test "$X" = 1 && test -n "$PATH"', env={"X": "1"}, timeout=30)
        if environment.exit_code != 0:
            raise RuntimeError("Workspace check failed its environment inheritance check.")
        await sandbox.write_file(nested, b"created by API\n")
        if await sandbox.read_file(nested) != b"created by API\n":
            raise RuntimeError("Workspace check failed to create missing parent directories.")
        ownership = await tools["sandbox_shell"](
            command=(
                "python3 -c "
                + shlex.quote("import os; assert os.stat(" + repr(nested) + ").st_uid == os.getuid()")
                + " && printf '%s' 'changed by shell' >> "
                + shlex.quote(nested)
            ),
            tool_context=None,
            timeout=30,
        )
        if ownership["exit_code"] != 0 or await sandbox.read_file(nested) != b"created by API\nchanged by shell":
            raise RuntimeError(
                "Workspace check failed: API-written files must belong to and be writable by the shell user."
            )
        created = await sandbox.execute(
            "printf '%s' "
            + shlex.quote("#!/bin/sh\nprintf '%s' before\n")
            + " > "
            + shlex.quote(script)
            + " && chmod 0755 "
            + shlex.quote(script),
            timeout=30,
        )
        if created.exit_code != 0:
            raise RuntimeError("Workspace check could not create its executable script.")
        await tools["sandbox_file_editor"](
            command="str_replace",
            path=script,
            old_str="before",
            new_str="after",
            tool_context=None,
        )
        mode_check = await sandbox.execute(
            "python3 -c "
            + shlex.quote("import os,stat; assert stat.S_IMODE(os.stat(" + repr(script) + ").st_mode) == 0o755"),
            timeout=30,
        )
        executed = await tools["sandbox_shell"](command=shlex.quote(script), tool_context=None, timeout=30)
        if mode_check.exit_code != 0 or executed["exit_code"] != 0 or executed["output"] != "after":
            raise RuntimeError("Workspace check failed: editing a 0755 script must preserve its executable mode.")
    finally:
        cleanup = await sandbox.execute("rm -rf -- " + quoted + " " + shlex.quote(directory), timeout=30)
        if cleanup.exit_code != 0:
            raise RuntimeError("Workspace check could not remove its temporary files.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprite", default=os.environ.get("SPRITE_NAME"))
    parser.add_argument("--working-dir", default="/home/sprite")
    args = parser.parse_args()
    if not args.sprite:
        parser.error("Set SPRITE_NAME or pass --sprite.")
    token = os.environ.get("SPRITES_TOKEN")
    if not token:
        parser.error("Set SPRITES_TOKEN in your environment.")
    async with AsyncSpritesClient(token=token) as client:
        sandbox = SpritesSandbox(client.sprite(args.sprite), working_dir=args.working_dir)
        await check_workspace(sandbox)
    print(
        "Workspace check passed: persistent files, 1 MiB transfer, environment inheritance, "
        "parent creation, file ownership, shell writes, and executable mode preservation."
    )


if __name__ == "__main__":
    asyncio.run(main())
