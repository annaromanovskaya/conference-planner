import asyncio

import uvicorn


class MCPServerLifecycle:
    def __init__(self, app, *, host: str, port: int, startup_timeout: float = 10) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                lifespan="on",
            )
        )
        self.startup_timeout = startup_timeout
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "MCPServerLifecycle":
        self._task = asyncio.create_task(self.server.serve(), name="confplanner-mcp")
        try:
            async with asyncio.timeout(self.startup_timeout):
                while not self.server.started:
                    if self._task.done():
                        await self._task
                        raise RuntimeError("MCP server stopped during startup")
                    await asyncio.sleep(0.01)
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def close(self) -> None:
        if self._task is None:
            return
        self.server.should_exit = True
        try:
            await self._task
        finally:
            self._task = None
