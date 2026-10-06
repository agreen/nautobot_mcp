"""Backward-compatible stdio entrypoint.

The implementation lives in ``main.py`` (``python main.py --mode stdio`` is equivalent).
"""

import asyncio

from main import run_stdio_mode


async def main():
    await run_stdio_mode()


if __name__ == "__main__":
    asyncio.run(main())
