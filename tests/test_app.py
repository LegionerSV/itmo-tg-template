import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, PhotoSize, Update, User

from app.config import ConfigError, Settings
from app.handlers.echo import router
from app.health import HealthState, health_result
from app.logging_setup import SecretFilter
from app.telegram import create_bot

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"BOT_TOKEN={TOKEN}\nPOSTGRES_PASSWORD=secret-db\n", encoding="utf-8")
    return Settings.load(path, environ={})


@pytest.mark.parametrize("text", ["Привет 👋", "/start", "<b>текст</b> & *слово*", "строка\nдва"])
async def test_echo_preserves_exact_text(text):
    # Arrange
    bot = Bot(TOKEN)
    bot.session = AsyncMock()
    dispatcher = Dispatcher()
    # A Router can have only one parent; use its handler callback through a fresh router.
    from aiogram import Router

    from app.handlers.echo import echo_text

    test_router = Router()
    from aiogram import F

    test_router.message.register(echo_text, F.text)
    dispatcher.include_router(test_router)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Студент"),
        text=text,
    )
    # Act
    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    # Assert
    method = bot.session.call_args.args[1]
    assert isinstance(method, SendMessage)
    assert method.chat_id == 42
    assert method.text == text
    assert method.parse_mode is None


async def test_photo_does_not_match_text_handler():
    # Arrange
    handler = router.message.handlers[0]
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        photo=[PhotoSize(file_id="a", file_unique_id="b", width=1, height=1)],
    )
    # Act
    matches, _ = await handler.check(message)
    # Assert
    assert matches is False


def test_settings_environment_overrides_file(tmp_path):
    # Arrange
    path = tmp_path / ".env"
    path.write_text(
        f"BOT_TOKEN={TOKEN}\nPOSTGRES_PASSWORD='p$a#ss'\nPOSTGRES_PORT=5432\n", encoding="utf-8"
    )
    # Act
    config = Settings.load(path, environ={"POSTGRES_PORT": "55432"})
    # Assert
    assert config.postgres_port == 55432
    assert config.postgres_password == "p$a#ss"
    assert "p$a#ss" not in repr(config)
    assert TOKEN not in repr(config)


@pytest.mark.parametrize("value", ["abc", "0", "65536"])
def test_invalid_port_has_safe_error(tmp_path, value):
    # Arrange
    path = tmp_path / ".env"
    # Act / Assert
    with pytest.raises(ConfigError, match="POSTGRES_PORT"):
        Settings.load(
            path,
            environ={"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "secret", "POSTGRES_PORT": value},
        )


@pytest.mark.parametrize("proxy", ["http://user:pass@localhost:8080", "socks5://localhost:1080"])
async def test_proxy_is_attached_to_bot_session(settings, proxy):
    # Arrange
    from dataclasses import replace

    config = replace(settings, telegram_proxy_url=proxy)
    # Act
    bot = create_bot(config)
    # Assert
    assert bot.session._proxy == proxy
    await bot.session.close()


def test_bad_proxy_does_not_leak_credentials(tmp_path):
    # Arrange
    proxy = "ftp://user:very-secret@host:123"
    # Act / Assert
    with pytest.raises(ConfigError) as exc:
        Settings.load(
            tmp_path / ".env",
            environ={"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "db", "TELEGRAM_PROXY_URL": proxy},
        )
    assert "TELEGRAM_PROXY_URL" in str(exc.value)
    assert "very-secret" not in str(exc.value)


async def test_health_tracks_pool_failure_and_recovery():
    # Arrange
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)
    task = asyncio.create_task(asyncio.Event().wait())
    state = HealthState(pool=pool, initialized=True, polling_task=task)
    try:
        # Act / Assert
        assert (await health_result(state))[0] == 200
        pool.fetchval.assert_awaited_with("SELECT 1", timeout=3)
        pool.fetchval.side_effect = ConnectionError("secret-password")
        status, body = await health_result(state)
        assert status == 503
        assert body == {"status": "not_ready", "database": "unavailable", "polling": "running"}
        pool.fetchval.side_effect = None
        assert (await health_result(state))[0] == 200
        state.initialized = False
        assert (await health_result(state))[0] == 503
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_health_without_polling_is_not_ready():
    # Arrange
    state = HealthState(pool=None)
    # Act
    status, body = await health_result(state)
    # Assert
    assert status == 503
    assert body["polling"] == "stopped"


def test_log_filter_redacts_secrets_and_exception():
    # Arrange
    secret_filter = SecretFilter([TOKEN, "db-secret", "proxy-secret"])
    try:
        raise ValueError("proxy-secret")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test",
            logging.ERROR,
            "",
            1,
            "Ошибка %s https://api.telegram.org/bot%s",
            ("db-secret", TOKEN),
            sys.exc_info(),
        )
    # Act
    secret_filter.filter(record)
    rendered = logging.Formatter().format(record)
    # Assert
    assert all(secret not in rendered for secret in (TOKEN, "db-secret", "proxy-secret"))
    assert "ValueError" in rendered
