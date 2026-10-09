"""Run a Strands agent using an existing Sprite and the default model provider."""

import asyncio
import os

from sprites import AsyncSpritesClient
from strands import Agent

from strands_sprites import SpritesSandbox


async def main() -> None:
    async with AsyncSpritesClient(token=os.environ["SPRITES_TOKEN"]) as client:
        sandbox = SpritesSandbox(
            client.sprite(os.environ["SPRITE_NAME"]),
            working_dir="/home/sprite",
        )
        agent = Agent(sandbox=sandbox)
        await agent.invoke_async("List the files in the Sprite's working directory.")


if __name__ == "__main__":
    asyncio.run(main())
