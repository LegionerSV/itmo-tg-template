import argparse
from pathlib import Path

from app.config import ConfigError
from scripts.cloud import cloud_main
from scripts.common import CommandError
from scripts.local import local_main


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Настройка учебного Telegram-бота")
    commands = root.add_subparsers(dest="mode", required=True)
    local = commands.add_parser("local", help="Локальная разработка")
    local.add_argument(
        "action", nargs="?", default="up", choices=["up", "stop", "status", "logs", "health"]
    )
    local.add_argument("--setup-only", action="store_true", help="Подготовить .venv и БД для IDE")
    cloud = commands.add_parser("cloud", help="Развёртывание в Yandex Cloud")
    cloud.add_argument(
        "action",
        nargs="?",
        default="up",
        choices=["up", "status", "logs", "stop", "start", "destroy", "access"],
    )
    cloud.add_argument("--folder-id", help="Каталог Yandex Cloud; по умолчанию из yc init")
    cloud.add_argument("--ssh-cidr", help="Публичный IPv4/CIDR студента, например 203.0.113.10/32")
    cloud.add_argument("--zone", help="Зона новой ВМ; по умолчанию ru-central1-a")
    cloud.add_argument("--platform", help="Платформа новой ВМ; по умолчанию standard-v2")
    cloud.add_argument("--cores", type=int, help="vCPU новой ВМ; по умолчанию 2")
    cloud.add_argument("--core-fraction", type=int, help="Гарантия vCPU, %%; по умолчанию 5")
    cloud.add_argument("--memory", type=int, help="RAM новой ВМ, ГБ; по умолчанию 1")
    cloud.add_argument("--disk-size", type=int, help="HDD новой ВМ, ГБ; по умолчанию 20")
    return root


def main() -> int:
    args = parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if args.mode == "local":
            return local_main(root, args.action, args.setup_only)
        return cloud_main(root, args)
    except (CommandError, ConfigError) as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("Операция прервана.")
        return 130
    except Exception:
        # Не печатаем неизвестные исключения: они могут содержать секретные значения.
        print("Операция не выполнена. Проверьте доступ к файлам, сеть и настройки по README.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
