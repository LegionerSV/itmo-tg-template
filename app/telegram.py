from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from app.config import Settings


def create_bot(settings: Settings) -> Bot:
    # Одна сессия для getMe, polling и ответов; прямого fallback нет.
    session = AiohttpSession(proxy=settings.telegram_proxy_url or None, timeout=40)
    return Bot(token=settings.bot_token, session=session)
