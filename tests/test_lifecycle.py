import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession

from app.config import Settings
from app.health import HealthState, start_health_server


async def test_http_health_returns_503_then_200(unused_tcp_port):
    # Arrange
    pool = Mock(fetchval=AsyncMock(return_value=1))
    state = HealthState(pool=pool)
    runner = await start_health_server(state, unused_tcp_port)
    task = asyncio.create_task(asyncio.Event().wait())
    try:
        async with ClientSession() as session:
            # Act / Assert
            async with session.get(f"http://127.0.0.1:{unused_tcp_port}/health") as response:
                assert response.status == 503
            state.initialized, state.polling_task = True, task
            async with session.get(f"http://127.0.0.1:{unused_tcp_port}/health") as response:
                assert response.status == 200
                assert await response.json() == {
                    "status": "ok",
                    "database": "ok",
                    "polling": "running",
                }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()


async def test_telegram_failure_closes_pool_and_session(monkeypatch):
    # Arrange
    from app import __main__ as application

    pool = Mock(close=AsyncMock())
    bot = Mock(
        get_me=AsyncMock(side_effect=ConnectionError("proxy offline")),
        session=Mock(close=AsyncMock()),
    )
    monkeypatch.setattr(application, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))
    settings = Settings(bot_token="unused", postgres_password="unused")
    # Act
    with pytest.raises(ConnectionError):
        await application.run(settings)
    # Assert
    pool.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()


async def test_db_failure_closes_http_session(monkeypatch):
    # Arrange
    from app import __main__ as application

    bot = Mock(session=Mock(close=AsyncMock()))
    monkeypatch.setattr(application, "create_pool", AsyncMock(side_effect=ConnectionError))
    monkeypatch.setattr(application, "create_bot", Mock(return_value=bot))
    # Act
    with pytest.raises(ConnectionError):
        await application.run(Settings(bot_token="unused", postgres_password="unused"))
    # Assert
    bot.session.close.assert_awaited_once()


async def test_http_proxy_receives_connect_and_failure_has_no_direct_fallback(unused_tcp_port):
    # Arrange: локальный HTTP CONNECT-прокси отклоняет авторизацию.
    from aiohttp_socks import ProxyError

    from app.telegram import create_bot

    requests = []

    async def proxy(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            requests.append(header)
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(proxy, "127.0.0.1", unused_tcp_port)
    bot = create_bot(
        Settings(
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            postgres_password="unused",
            telegram_proxy_url=f"http://student:password@127.0.0.1:{unused_tcp_port}",
        )
    )
    try:
        # Act / Assert
        with pytest.raises(ProxyError, match="407"):
            async with asyncio.timeout(5):
                await bot.get_me()
        assert requests[0].startswith(b"CONNECT api.telegram.org:443 HTTP/1.1\r\n")
        assert b"Proxy-Authorization: Basic " in requests[0]
    finally:
        await bot.session.close()
        server.close()
        await server.wait_closed()
