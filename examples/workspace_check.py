"""Verify persistent Sprite files with two commands and no model call."""

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
    try:
        written = await sandbox.execute(f"printf '%s' 'persistent workspace' > {quoted}", timeout=30)
        if written.exit_code != 0:
            raise RuntimeError("Workspace check could not write its temporary file.")
        read = await sandbox.execute(f"cat {quoted}", timeout=30)
        if read.exit_code != 0 or read.stdout != "persistent workspace":
            raise RuntimeError("Workspace check could not read the first command's file.")
    finally:
        await sandbox.remove_file(path)


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
    print("Workspace check passed: the second command read the first command's file.")


if __name__ == "__main__":
    asyncio.run(main())
