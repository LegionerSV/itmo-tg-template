"""Проверка конфигурации требует только Docker CLI, не запущенный Engine."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.common import compose_quote
from scripts.local import compose_command


@pytest.mark.skipif(not shutil.which("docker"), reason="Docker CLI не установлен")
def test_local_and_cloud_compose_ports_credentials_and_healthchecks(tmp_path):
    # Arrange
    root = Path(__file__).resolve().parents[1]
    password = "p$a#s's\\\\tail"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=test\nPOSTGRES_PORT=55432\nPOSTGRES_PASSWORD=" + compose_quote(password) + "\n",
        encoding="utf-8",
    )
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("POSTGRES_", "BOT_TOKEN", "COMPOSE_"))
    }
    command = compose_command(root)
    command[command.index("--env-file") + 1] = str(env_file)

    def config(command):
        result = subprocess.run(
            [*command, "config", "--format", "json"],
            env=environ,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert result.returncode == 0, "Docker Compose не смог разобрать конфигурацию"
        return json.loads(result.stdout)["services"]

    # Act
    local = config(command)
    cloud = config(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(root / "compose.yaml"),
            "--profile",
            "cloud",
        ]
    )
    # Assert
    assert local["db"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert local["db"]["ports"][0]["published"] == "55432"
    # config экранирует доллар для повторного чтения сохранённого Compose YAML/JSON.
    assert local["db"]["environment"]["POSTGRES_PASSWORD"].replace("$$", "$") == password
    assert all(not service.get("ports") for service in cloud.values())
    assert cloud["bot"]["environment"]["POSTGRES_HOST"] == "db"
    assert cloud["bot"]["environment"]["POSTGRES_PORT"] == "5432"
    assert "app.healthcheck" in cloud["bot"]["healthcheck"]["test"]
    assert cloud["bot"]["healthcheck"]["timeout"] == "20s"
    assert cloud["bot"]["depends_on"]["db"]["condition"] == "service_healthy"
