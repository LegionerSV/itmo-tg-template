import asyncio
from dataclasses import dataclass

import asyncpg
from aiohttp import web


@dataclass
class HealthState:
    pool: asyncpg.Pool | None = None
    initialized: bool = False
    polling_task: asyncio.Task | None = None


async def health_result(state: HealthState) -> tuple[int, dict[str, str]]:
    database_ok = False
    if state.pool is not None:
        try:
            database_ok = await state.pool.fetchval("SELECT 1", timeout=3) == 1
        except Exception:
            # Не возвращаем клиенту исключения драйвера и параметры подключения.
            pass
    polling_ok = state.polling_task is not None and not state.polling_task.done()
    ready = state.initialized and polling_ok and database_ok
    return (200 if ready else 503), {
        "status": "ok" if ready else "not_ready",
        "database": "ok" if database_ok else "unavailable",
        "polling": "running" if polling_ok else "stopped",
    }


async def start_health_server(state: HealthState, port: int) -> web.AppRunner:
    async def health(_request: web.Request) -> web.Response:
        status, body = await health_result(state)
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        # Docker HEALTHCHECK выполняется внутри контейнера, внешний порт не нужен.
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
