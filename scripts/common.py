import os
import subprocess
from pathlib import Path


class CommandError(RuntimeError):
    pass


def compose_quote(value: str) -> str:
    """Compose сохраняет обратные косые черты внутри одинарных кавычек буквально."""
    return "'" + value.replace("'", "\\'") + "'"


def run_command(
    args: list[str],
    *,
    label: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
    interactive: bool = False,
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            capture_output=not interactive,
        )
    except FileNotFoundError:
        raise CommandError(f"{label}: программа не найдена; проверьте установку и PATH.") from None
    except subprocess.TimeoutExpired:
        raise CommandError(f"{label}: превышено время ожидания ({timeout} с).") from None
    except OSError:
        raise CommandError(f"{label}: не удалось запустить программу.") from None
    if result.returncode:
        # Вывод сторонних программ может содержать env и токены; не печатаем его.
        raise CommandError(f"{label}: команда завершилась с кодом {result.returncode}.")
    return result.stdout or ""


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    path.chmod(0o600)
