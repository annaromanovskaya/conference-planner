import asyncio

from conference_planner.app import run_application
from conference_planner.config import Settings


def run() -> None:
    asyncio.run(run_application(Settings()))


if __name__ == "__main__":
    run()
