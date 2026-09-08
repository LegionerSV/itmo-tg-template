from aiogram import F, Router
from aiogram.types import Message

router = Router(name="echo")


@router.message(F.text)
async def echo_text(message: Message) -> None:
    # Общий обработчик регистрируется после обработчиков команд лабораторных.
    # Пул БД доступен через аргумент db: asyncpg.Pool при необходимости.
    await message.answer(message.text, parse_mode=None)
