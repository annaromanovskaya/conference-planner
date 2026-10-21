import asyncio

from conference_scout.app import run_application
from conference_scout.config import Settings


def run() -> None:
    asyncio.run(run_application(Settings()))


if __name__ == "__main__":
    run()
