import subprocess
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values

from scripts.common import CommandError, run_command
from scripts.local import compose_command, prepare_env


def test_first_setup_and_repeat_preserve_secrets(tmp_path):
    # Arrange
    root = tmp_path / "Курс с пробелами"
    root.mkdir()
    prompt = Mock(side_effect=["123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", ""])
    # Act
    path = prepare_env(root, cloud=False, ask=prompt, environ={})
    first = path.read_bytes()
    prepare_env(root, cloud=False, ask=Mock(side_effect=AssertionError), environ={})
    # Assert
    assert path.read_bytes() == first
    assert len(dotenv_values(path)["POSTGRES_PASSWORD"]) >= 24
    assert dotenv_values(path)["POSTGRES_HOST"] == "127.0.0.1"


def test_cloud_requires_proxy_and_has_separate_password(tmp_path):
    # Arrange
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
    prepare_env(tmp_path, cloud=False, ask=Mock(side_effect=[token, ""]), environ={})
    # Act
    path = prepare_env(
        tmp_path,
        cloud=True,
        ask=Mock(side_effect=[token, "socks5://user:p%40ss@proxy:1080"]),
        environ={},
    )
    # Assert
    config = dotenv_values(path)
    assert config["POSTGRES_HOST"] == "db"
    assert config["POSTGRES_PASSWORD"] != dotenv_values(tmp_path / ".env")["POSTGRES_PASSWORD"]
    assert config["TELEGRAM_PROXY_URL"] == "socks5://user:p%40ss@proxy:1080"


def test_cloud_empty_proxy_fails_before_creating_file(tmp_path):
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="прокси"):
        prepare_env(
            tmp_path,
            cloud=True,
            ask=Mock(side_effect=["123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", ""]),
            environ={},
        )
    assert not (tmp_path / ".env.cloud").exists()


def test_compose_uses_absolute_paths(tmp_path):
    # Arrange
    root = tmp_path / "Проект с пробелами"
    # Act
    command = compose_command(root)
    # Assert
    assert str(root / "compose.yaml") in command
    assert str(root / ".env") in command
    assert command[:2] == ["docker", "compose"]
    assert command[command.index("--project-name") + 1].isascii()


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("secret", 1)])
def test_command_failure_has_safe_message(monkeypatch, failure):
    # Arrange
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=failure))
    # Act / Assert
    with pytest.raises(CommandError) as exc:
        run_command(["tool", "secret"], label="Проверка инструмента")
    assert "secret" not in str(exc.value)
    assert "Проверка инструмента" in str(exc.value)


def test_command_stderr_not_leaked(monkeypatch):
    # Arrange
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess(["tool"], 1, "secret-out", "secret-error")),
    )
    # Act / Assert
    with pytest.raises(CommandError) as exc:
        run_command(["tool"], label="Ошибка инструмента")
    assert "secret" not in str(exc.value)


class FakeYC:
    def __init__(self):
        self.resources = {}
        self.created = []
        self.deleted = []
        self.fail_after_create = None

    def __call__(self, args, **kwargs):
        kind = tuple(args[:2])
        operation = args[2]
        if operation == "list":
            return list(self.resources.get(kind, {}).values())
        if operation == "create":
            name = args[args.index("--name") + 1]
            label = args[args.index("--labels") + 1].split("=", 1)[1]
            resource = {
                "id": f"id-{len(self.created)}",
                "name": name,
                "folder_id": "folder-1",
                "labels": {"template-id": label},
            }
            self.resources.setdefault(kind, {})[resource["id"]] = resource
            self.created.append(kind)
            if self.fail_after_create == kind:
                self.fail_after_create = None
                raise CommandError("Соединение прервано после создания")
            return resource
        if operation == "get":
            return self.resources[kind][args[args.index("--id") + 1]]
        if operation == "delete":
            rid = args[args.index("--id") + 1]
            del self.resources[kind][rid]
            self.deleted.append((kind, rid))
            return {}
        raise AssertionError(args)


def cloud(tmp_path, fake):
    from scripts.cloud import Cloud

    return Cloud(tmp_path, folder_id="folder-1", yc=fake)


def test_cloud_reuses_resources_after_restart(tmp_path):
    # Arrange
    fake = FakeYC()
    first = cloud(tmp_path, fake)
    # Act
    original = first.ensure("network", ["vpc", "network"], [])
    resumed = cloud(tmp_path, fake)
    again = resumed.ensure("network", ["vpc", "network"], [])
    # Assert
    assert again["id"] == original["id"]
    assert len(fake.created) == 1


def test_cloud_recovers_ambiguous_creation(tmp_path):
    # Arrange
    fake = FakeYC()
    fake.fail_after_create = ("vpc", "network")
    deployment = cloud(tmp_path, fake)
    # Act
    with pytest.raises(CommandError):
        deployment.ensure("network", ["vpc", "network"], [])
    resumed = cloud(tmp_path, fake)
    found = resumed.ensure("network", ["vpc", "network"], [])
    # Assert
    assert found["id"] == "id-0"
    assert len(fake.created) == 1


def test_cloud_refuses_foreign_resource_and_folder(tmp_path):
    # Arrange
    fake = FakeYC()
    deployment = cloud(tmp_path, fake)
    item = deployment.ensure("network", ["vpc", "network"], [])
    item["labels"] = {"template-id": "someone-else"}
    # Act / Assert
    with pytest.raises(CommandError, match="принадлеж"):
        deployment.destroy(confirmed=True)
    assert not fake.deleted
    with pytest.raises(CommandError, match="каталог"):
        from scripts.cloud import Cloud

        Cloud(tmp_path, folder_id="another-folder", yc=fake)


def test_delete_requires_confirmation_and_only_deletes_owned_ids(tmp_path):
    # Arrange
    fake = FakeYC()
    deployment = cloud(tmp_path, fake)
    item = deployment.ensure("network", ["vpc", "network"], [])
    fake.resources[("vpc", "network")]["foreign"] = {
        "id": "foreign",
        "name": "foreign",
        "labels": {},
        "folder_id": "folder-1",
    }
    # Act
    with pytest.raises(CommandError, match="подтверждение"):
        deployment.destroy(confirmed=False)
    deployment.destroy(confirmed=True)
    # Assert
    assert fake.deleted == [(("vpc", "network"), item["id"])]
    assert "foreign" in fake.resources[("vpc", "network")]


def test_deploy_archive_never_contains_env_or_keys(tmp_path):
    # Arrange
    import tarfile

    from scripts.cloud import source_archive

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / ".env").write_text("SECRET=yes", encoding="utf-8")
    (tmp_path / ".env.cloud").write_text("SECRET=yes", encoding="utf-8")
    (tmp_path / ".deploy").mkdir()
    (tmp_path / ".deploy" / "id_ed25519").write_text("PRIVATE", encoding="utf-8")
    # Act
    path = source_archive(tmp_path)
    with tarfile.open(path) as archive:
        names = archive.getnames()
    # Assert
    assert "app/__init__.py" in names
    assert all(".env" not in n and "id_ed25519" not in n for n in names)


def test_missing_cloud_key_is_not_replaced(tmp_path):
    # Arrange
    deployment = cloud(tmp_path, FakeYC())
    deployment.state["resources"]["vm"] = {"id": "existing", "name": "existing"}
    # Act / Assert
    with pytest.raises(CommandError, match="ключ"):
        deployment.key()
    assert not (tmp_path / ".deploy" / "id_ed25519").exists()


def test_provision_builds_private_network_and_nonpreemptible_vm(tmp_path, monkeypatch):
    # Arrange
    from scripts.cli import parser

    fake = FakeYC()
    deployment = cloud(tmp_path, fake)
    key = tmp_path / ".deploy" / "id_ed25519"
    key.write_text("test-private-key", encoding="utf-8")
    key.with_suffix(".pub").write_text("ssh-ed25519 test-public-key", encoding="utf-8")
    calls = []
    original = deployment.yc

    def recording(args):
        calls.append(args)
        return original(args)

    deployment.yc = recording
    args = parser().parse_args(["cloud", "up", "--ssh-cidr", "203.0.113.10/32"])
    # Act
    deployment.provision(args)
    deployment.provision(args)
    # Assert
    assert len(fake.created) == 4
    vm = next(c for c in calls if c[:3] == ["compute", "instance", "create"])
    assert "--preemptible" not in vm
    assert vm[vm.index("--core-fraction") + 1] == "5"
    assert "auto-delete=true" in vm[vm.index("--create-boot-disk") + 1]
    group = next(c for c in calls if c[:3] == ["vpc", "security-group", "create"])
    assert "direction=ingress,protocol=tcp,port=22,v4-cidrs=203.0.113.10/32" in group
    initialization = (tmp_path / ".deploy" / "cloud-init.yaml").read_text(encoding="utf-8")
    assert "test-private-key" not in initialization
    assert "BOT_TOKEN" not in initialization
    assert "swapfile" in initialization


def test_deploy_uploads_secrets_only_over_ssh_stdin(tmp_path, monkeypatch):
    # Arrange
    import base64

    from app.config import Settings

    deployment = cloud(tmp_path, FakeYC())
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    calls = []

    def ssh(instance, command, **kwargs):
        calls.append((command, kwargs))
        return "Готово"

    monkeypatch.setattr(deployment, "ssh", ssh)
    monkeypatch.setattr(deployment, "wait_ssh", Mock())
    settings = Settings(
        bot_token="secret-token",
        postgres_password="p$a#s's",
        telegram_proxy_url="http://user:password@proxy:3128",
    )
    # Act
    deployment.deploy({}, settings)
    # Assert
    assert all("secret-token" not in command for command, _ in calls)
    env_payload = next(
        kw["input_text"] for cmd, kw in calls if "course-env.pending" in cmd and "base64" in cmd
    )
    from io import StringIO

    values = dotenv_values(
        stream=StringIO(base64.b64decode(env_payload).decode()), interpolate=False
    )
    assert values["BOT_TOKEN"] == "secret-token"
    assert values["POSTGRES_PASSWORD"] == "p$a#s's"
    assert values["POSTGRES_HOST"] == "db"
    assert any("--wait --wait-timeout 180" in cmd for cmd, _ in calls)
    assert not (tmp_path / ".deploy" / "source.tar.gz").exists()


def test_cloud_help_renders(capsys):
    # Arrange
    from scripts.cli import parser

    # Act
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["cloud", "--help"])
    # Assert
    assert exc.value.code == 0
    assert "Гарантия vCPU, %" in capsys.readouterr().out


def test_setup_only_checks_real_credentials_without_starting_bot(tmp_path, monkeypatch):
    # Arrange
    from unittest.mock import AsyncMock

    from scripts import local

    prepare_env(
        tmp_path,
        cloud=False,
        ask=Mock(side_effect=["123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", ""]),
        environ={},
    )
    commands = Mock(return_value="")
    database = AsyncMock()
    launch = Mock()
    monkeypatch.setattr(local, "run_command", commands)
    monkeypatch.setattr(local, "check_database", database)
    monkeypatch.setattr(local.subprocess, "call", launch)
    # Act
    result = local.local_main(tmp_path, "up", setup_only=True)
    # Assert
    assert result == 0
    assert "--wait" in commands.call_args.args[0]
    database.assert_awaited_once()
    launch.assert_not_called()


def test_custom_vm_configuration_is_remembered(tmp_path):
    # Arrange
    from scripts.cli import parser

    fake = FakeYC()
    deployment = cloud(tmp_path, fake)
    key = tmp_path / ".deploy" / "id_ed25519"
    key.write_text("private-test", encoding="utf-8")
    key.with_suffix(".pub").write_text("ssh-ed25519 public-test", encoding="utf-8")
    first = parser().parse_args(["cloud", "--ssh-cidr", "203.0.113.10/32", "--memory", "2"])
    # Act
    deployment.provision(first)
    deployment.provision(parser().parse_args(["cloud"]))
    # Assert
    assert deployment.state["config"]["memory"] == 2
    assert len(fake.created) == 4
