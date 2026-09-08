"""Проверка уже работающего приложения: python -m app.healthcheck."""

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from app.config import ConfigError, Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить приложение и подключение БД")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    try:
        settings = Settings.load(args.env_file)
        # Системный HTTP_PROXY не должен перехватывать локальную проверку.
        with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{settings.health_port}/health", timeout=5
        ) as response:
            body = json.load(response)
        if body.get("status") != "ok":
            raise ValueError
    except ConfigError as exc:
        print(str(exc))
        return 1
    except (HTTPError, URLError, OSError, ValueError):
        print("Приложение не готово: проверьте запуск бота, подключение БД и логи.")
        return 1
    print("Приложение готово: polling работает, PostgreSQL отвечает на SELECT 1.")
    print("Доступность Telegram при сетевых сбоях проверяйте по логам бота.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
