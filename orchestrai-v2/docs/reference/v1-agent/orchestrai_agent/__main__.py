"""Agent entrypoint. `python -m orchestrai_agent` boots an agent and runs
until SIGTERM."""

import asyncio
import logging
import signal
import sys

from orchestrai_agent.loop import AgentLoop


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        stream=sys.stdout,
    )
    # quiet down httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _async_main() -> int:
    _configure_logging()
    log = logging.getLogger("orchestrai-agent")
    loop = AgentLoop()

    def _shutdown(*_):
        log.info("received shutdown signal — releasing tasks")
        loop.request_stop()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _shutdown)
            except (ValueError, OSError):
                pass  # not the main thread; ignore
    except Exception:
        pass

    await loop.run()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
