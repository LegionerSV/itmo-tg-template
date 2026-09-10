"""Одна учебная ВМ. Ресурсы привязаны к локальному state и метке template-id."""

import base64
import ipaddress
import json
import os
import secrets
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from urllib.request import urlopen

from app.config import Settings
from scripts.common import CommandError, compose_quote, private_write, run_command
from scripts.local import prepare_env

KINDS = {
    "network": ["vpc", "network"],
    "subnet": ["vpc", "subnet"],
    "security-group": ["vpc", "security-group"],
    "vm": ["compute", "instance"],
}
REMOTE = "/home/student/course-bot"
COMPOSE = f"cd {REMOTE} && docker compose --env-file .env.cloud --profile cloud"


def install_yc() -> str:
    found = shutil.which("yc")
    usual = Path.home() / "yandex-cloud" / "bin" / ("yc.exe" if os.name == "nt" else "yc")
    if found or usual.exists():
        return found or str(usual)
    print("Устанавливаем Yandex Cloud CLI официальным установщиком…", flush=True)
    extension = "ps1" if os.name == "nt" else "sh"
    with tempfile.TemporaryDirectory(prefix="itmo-yc-") as directory:
        installer = Path(directory) / f"install.{extension}"
        with urlopen(
            f"https://storage.yandexcloud.net/yandexcloud-yc/install.{extension}", timeout=60
        ) as response:
            installer.write_bytes(response.read())
        command = (
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
            if os.name == "nt"
            else ["bash"]
        )
        run_command([*command, str(installer)], label="Установка yc", interactive=True, timeout=600)
    if not usual.exists():
        raise CommandError("yc не найден после установки. Добавьте его каталог в PATH и повторите.")
    return str(usual)


def authenticate(yc_path: str) -> str:
    try:
        run_command([yc_path, "iam", "create-token"], label="Проверка авторизации yc")
    except CommandError:
        run_command([yc_path, "init"], label="Вход в Yandex Cloud", interactive=True, timeout=1800)
    folder = run_command(
        [yc_path, "config", "get", "folder-id"], label="Чтение каталога yc"
    ).strip()
    if not folder:
        raise CommandError("Выполните yc init и выберите каталог с подключённым биллингом.")
    return folder


def source_archive(root: Path) -> Path:
    deployment = root / ".deploy"
    deployment.mkdir(exist_ok=True)
    archive_path = deployment / "source.tar.gz"
    # Явный список предотвращает отправку .env, .venv, ключей и чужих файлов.
    candidates = [
        root / name
        for name in [
            "Dockerfile",
            ".dockerignore",
            "requirements.txt",
            "constraints.txt",
            "compose.yaml",
        ]
    ]
    candidates.extend((root / "app").rglob("*.py"))
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in sorted(candidates):
            if path.is_file() and "__pycache__" not in path.parts and not path.is_symlink():
                archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    return archive_path


class Cloud:
    def __init__(self, root: Path, *, folder_id: str, yc: Callable):
        self.root, self.yc = root, yc
        self.directory = root / ".deploy"
        self.state_path = self.directory / "state.json"
        self.directory.mkdir(exist_ok=True)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state["folder_id"] != folder_id:
                raise CommandError("Состояние деплоя относится к другому каталогу Yandex Cloud.")
        else:
            self.state = {
                "version": 1,
                "folder_id": folder_id,
                "owner": secrets.token_hex(8),
                "resources": {},
            }
            self.save()

    def save(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        private_write(temporary, json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.state_path)

    def verify(self, resource: dict) -> None:
        if (
            resource.get("labels", {}).get("template-id") != self.state["owner"]
            or resource.get("folder_id") != self.state["folder_id"]
        ):
            raise CommandError("Ресурс не принадлежит этому проекту; операция отменена.")

    def lookup(self, key: str, kind: list[str]) -> dict | None:
        record = self.state["resources"].get(key)
        if not record:
            return None
        resources = self.yc([*kind, "list"])
        matches = [
            r
            for r in resources
            if (r["id"] == record["id"] if record.get("id") else r["name"] == record["name"])
        ]
        if len(matches) > 1:
            raise CommandError("Найдено несколько одноимённых ресурсов; проверьте консоль облака.")
        if matches:
            if record.get("managed", True):
                self.verify(matches[0])
            elif matches[0].get("folder_id") != self.state["folder_id"]:
                raise CommandError("Внешний ресурс относится к другому каталогу Yandex Cloud.")
            return matches[0]
        return None

    def ensure(self, key: str, kind: list[str], flags: list[str]) -> dict:
        name = f"itmo-{self.state['owner']}-{key}"
        self.state["resources"].setdefault(key, {"name": name})
        # Сохраняем намерение ДО запроса: после сетевого сбоя найдём созданный ресурс по имени.
        self.save()
        resource = self.lookup(key, kind)
        if resource is None:
            if self.state["resources"][key].get("id"):
                raise CommandError(
                    f"Ранее созданный {key} исчез. Проверьте состояние облака перед новым деплоем."
                )
            print(f"Создаём ресурс: {key}…", flush=True)
            resource = self.yc(
                [
                    *kind,
                    "create",
                    "--name",
                    name,
                    "--labels",
                    f"template-id={self.state['owner']}",
                    *flags,
                ]
            )
            self.verify(resource)
        self.state["resources"][key]["id"] = resource["id"]
        self.save()
        return resource

    def ensure_network(self) -> dict:
        resource = self.lookup("network", KINDS["network"])
        if resource is not None:
            return resource
        record = self.state["resources"].get("network")
        if record and record.get("id"):
            raise CommandError("Ранее использованная сеть исчезла. Проверьте состояние облака.")
        defaults = [
            network
            for network in self.yc([*KINDS["network"], "list"])
            if network.get("name") == "default"
        ]
        if len(defaults) > 1:
            raise CommandError("Найдено несколько сетей default; проверьте консоль облака.")
        if defaults:
            resource = defaults[0]
            if resource.get("folder_id") != self.state["folder_id"]:
                raise CommandError("Сеть default относится к другому каталогу Yandex Cloud.")
            self.state["resources"]["network"] = {
                "id": resource["id"],
                "name": resource["name"],
                "managed": False,
            }
            self.save()
            print("Используем существующую сеть default.", flush=True)
            return resource
        return self.ensure("network", KINDS["network"], [])

    def destroy(self, *, confirmed: bool) -> None:
        if not confirmed:
            raise CommandError("Необходимо явное подтверждение удаления БД и ресурсов.")
        # До первого удаления проверяем принадлежность всех оставшихся ресурсов.
        found = {key: self.lookup(key, kind) for key, kind in KINDS.items()}
        for key in reversed(KINDS):
            resource = found[key]
            record = self.state["resources"].get(key, {})
            if resource and record.get("managed", True):
                self.yc([*KINDS[key], "delete", "--id", resource["id"]])
            self.state["resources"].pop(key, None)
            self.save()
        self.state_path.unlink()
        print("Ресурсы проекта удалены. Облачная БД и загрузочный диск удалены вместе с ВМ.")

    def key(self) -> Path:
        key = self.directory / "id_ed25519"
        if not key.exists():
            if "vm" in self.state["resources"]:
                raise CommandError("Утерян SSH-ключ существующей ВМ. Восстановите его из копии.")
            run_command(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(key),
                    "-C",
                    f"itmo-{self.state['owner']}",
                ],
                label="Создание SSH-ключа",
            )
        if not key.with_suffix(".pub").exists():
            raise CommandError("Нет публичной части ключа .deploy/id_ed25519.pub.")
        return key

    def provision(self, args) -> dict:
        defaults = {
            "zone": "ru-central1-a",
            "platform": "standard-v2",
            "cores": 2,
            "core_fraction": 5,
            "memory": 1,
            "disk_size": 20,
        }
        config = {**defaults, **self.state.get("config", {})}
        config.update(
            {key: getattr(args, key) for key in defaults if getattr(args, key) is not None}
        )
        if "config" in self.state and config != self.state["config"]:
            raise CommandError(
                "Параметры существующей ВМ отличаются. Используйте прежние аргументы."
            )
        cidr = args.ssh_cidr or self.state.get("ssh_cidr")
        if not cidr:
            raise CommandError("Укажите --ssh-cidr ВАШ_ПУБЛИЧНЫЙ_IP/32 для доступа по SSH.")
        try:
            network = ipaddress.ip_network(cidr, strict=True)
            if network.version != 4 or network.prefixlen == 0:
                raise ValueError
        except ValueError:
            raise CommandError("--ssh-cidr: нужен IPv4/CIDR, например 203.0.113.10/32.") from None
        if self.state.get("ssh_cidr") and cidr != self.state["ssh_cidr"]:
            raise CommandError(
                "IP изменился: сначала выполните deploy access --ssh-cidr НОВЫЙ_IP/32."
            )
        self.state.update(config=config, ssh_cidr=cidr)
        self.save()
        key = self.key()
        network = self.ensure_network()
        subnet = self.ensure(
            "subnet",
            KINDS["subnet"],
            ["--network-id", network["id"], "--zone", config["zone"], "--range", "10.120.0.0/24"],
        )
        security = self.ensure(
            "security-group",
            KINDS["security-group"],
            [
                "--network-id",
                network["id"],
                "--rule",
                f"direction=ingress,protocol=tcp,port=22,v4-cidrs={cidr}",
                "--rule",
                "direction=egress,protocol=any,port=any,v4-cidrs=0.0.0.0/0",
            ],
        )
        cloud_init = self.directory / "cloud-init.yaml"
        initialization = {
            # Группа нужна до установки пакета Docker: users-groups выполняется раньше packages.
            "groups": ["docker"],
            "users": [
                {
                    "name": "student",
                    "groups": ["sudo", "docker"],
                    "shell": "/bin/bash",
                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                    "lock_passwd": True,
                    "ssh_authorized_keys": [key.with_suffix(".pub").read_text().strip()],
                }
            ],
            "ssh_pwauth": False,
            "disable_root": True,
            "package_update": True,
            "packages": ["docker.io", "docker-compose-v2"],
            "runcmd": [
                [
                    "bash",
                    "-ceu",
                    (
                        "systemctl enable --now docker\n"
                        "if [ ! -f /swapfile ]; then\n"
                        "  fallocate -l 1G /swapfile\n"
                        "  chmod 600 /swapfile\n"
                        "  mkswap /swapfile\n"
                        "  swapon /swapfile\n"
                        "  echo '/swapfile none swap sw 0 0' >> /etc/fstab\n"
                        "fi\n"
                    ),
                ]
            ],
        }
        # JSON — допустимое содержимое YAML; секретов приложения здесь нет.
        private_write(cloud_init, "#cloud-config\n" + json.dumps(initialization, indent=2) + "\n")
        return self.ensure(
            "vm",
            KINDS["vm"],
            [
                "--zone",
                config["zone"],
                "--platform",
                config["platform"],
                "--cores",
                str(config["cores"]),
                "--core-fraction",
                str(config["core_fraction"]),
                "--memory",
                str(config["memory"]),
                "--create-boot-disk",
                f"image-folder-id=standard-images,image-family=ubuntu-2404-lts,"
                f"type=network-hdd,size={config['disk_size']},auto-delete=true",
                "--network-interface",
                f"subnet-id={subnet['id']},nat-ip-version=ipv4,security-group-ids={security['id']}",
                "--metadata-from-file",
                f"user-data={cloud_init}",
            ],
        )

    def instance(self) -> dict:
        instance = self.lookup("vm", KINDS["vm"])
        if not instance:
            raise CommandError("ВМ проекта не найдена. Сначала выполните deploy up.")
        return self.yc(["compute", "instance", "get", "--id", instance["id"]])

    def ssh(
        self,
        instance: dict,
        command: str,
        *,
        timeout: int = 120,
        input_text: str | None = None,
        stream_output: bool = False,
    ) -> str:
        try:
            address = instance["network_interfaces"][0]["primary_v4_address"]["one_to_one_nat"][
                "address"
            ]
            ipaddress.IPv4Address(address)
        except (KeyError, IndexError, ValueError):
            raise CommandError("У ВМ нет публичного IPv4. Проверьте её запуск.") from None
        key = self.directory / "id_ed25519"
        if not key.exists():
            raise CommandError(
                "Утерян SSH-ключ .deploy/id_ed25519. Восстановите его из своей копии."
            )
        return run_command(
            [
                "ssh",
                "-i",
                str(key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f'UserKnownHostsFile="{(self.directory / "known_hosts").as_posix()}"',
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=3",
                f"student@{address}",
                command,
            ],
            label="SSH: проверьте IP/CIDR, ключ, cloud-init и доступность ВМ",
            timeout=timeout,
            input_text=input_text,
            interactive=stream_output,
        )

    def wait_ssh(self, instance: dict) -> None:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            try:
                self.ssh(instance, "true", timeout=20)
                return
            except CommandError:
                print("Ждём SSH на ВМ…", flush=True)
                time.sleep(5)
        raise CommandError("SSH не стал доступен за 10 минут; проверьте --ssh-cidr и сеть.")

    def deploy(self, instance: dict, settings: Settings) -> None:
        self.wait_ssh(instance)
        print("Ждём установки Docker на ВМ…", flush=True)
        self.ssh(
            instance,
            "sudo cloud-init status --wait && docker compose version",
            timeout=900,
            stream_output=True,
        )
        archive = source_archive(self.root)
        try:
            payload = base64.b64encode(archive.read_bytes()).decode("ascii")
            self.ssh(
                instance,
                "umask 077; base64 -d > /home/student/course-source.tar.gz",
                input_text=payload,
                timeout=180,
            )
        finally:
            archive.unlink(missing_ok=True)
        # Конфиг передаётся по stdin SSH, не через argv, Docker build или metadata.
        values = {key.upper(): str(value) for key, value in asdict(settings).items()}
        values.update(POSTGRES_HOST="db", POSTGRES_PORT="5432")
        content = "".join(f"{key}={compose_quote(value)}\n" for key, value in values.items())
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        self.ssh(
            instance, "umask 077; base64 -d > /home/student/course-env.pending", input_text=payload
        )
        print("Собираем приложение и проверяем его готовность…", flush=True)
        # Пересоздаём только код; именованный volume БД сохраняется.
        command = (
            "set -eu; "
            f"mkdir -p {REMOTE}; chmod 700 {REMOTE}; "
            f"if [ -f {REMOTE}/.env.cloud ]; then {COMPOSE} stop bot; fi; "
            f"rm -rf {REMOTE}/app; "
            f"tar -xzf /home/student/course-source.tar.gz -C {REMOTE}; "
            f"mv /home/student/course-env.pending {REMOTE}/.env.cloud; "
            f"chmod 600 {REMOTE}/.env.cloud; "
            "rm /home/student/course-source.tar.gz; "
            f"{COMPOSE} up -d --build --wait --wait-timeout 180"
        )
        self.ssh(instance, command, timeout=1200, stream_output=True)
        print(self.ssh(instance, f"{COMPOSE} exec -T bot python -m app.healthcheck"))
        print("Деплой завершён. Статус и логи: deploy status / deploy logs.")

    def access(self, cidr: str) -> None:
        try:
            parsed = ipaddress.ip_network(cidr, strict=True)
            if parsed.version != 4 or parsed.prefixlen == 0:
                raise ValueError
        except (ValueError, TypeError):
            raise CommandError("Для access укажите --ssh-cidr ВАШ_ПУБЛИЧНЫЙ_IP/32.") from None
        group = self.lookup("security-group", KINDS["security-group"])
        if not group:
            raise CommandError("Группа безопасности проекта не найдена.")
        full = self.yc(["vpc", "security-group", "get", "--id", group["id"]])
        flags = []
        for rule in full.get("rules", []):
            if rule.get("direction") == "INGRESS":
                flags.extend(["--delete-rule-id", rule["id"]])
        self.yc(
            [
                "vpc",
                "security-group",
                "update-rules",
                "--id",
                group["id"],
                *flags,
                "--add-rule",
                f"direction=ingress,protocol=tcp,port=22,v4-cidrs={cidr}",
            ]
        )
        self.state["ssh_cidr"] = cidr
        self.save()
        print("Доступ по SSH обновлён.")


def cloud_main(root: Path, args) -> int:
    for command in ["ssh", "ssh-keygen"]:
        if not shutil.which(command):
            raise CommandError(f"Установите OpenSSH: не найдена команда {command}.")
    settings = None
    if args.action == "up":
        settings = Settings.load(prepare_env(root, cloud=True))
    if args.action != "up" and not (root / ".deploy" / "state.json").exists():
        raise CommandError("Нет состояния деплоя. Сначала выполните deploy up.")
    yc_path = install_yc()
    folder = authenticate(yc_path)
    folder = args.folder_id or folder

    def yc(command, **kwargs):
        output = run_command(
            [yc_path, *command, "--folder-id", folder, "--format", "json"],
            label="Yandex Cloud: проверьте профиль, права, квоты и параметры ВМ",
            timeout=600,
            error_prefix="ERROR:",
        )
        return json.loads(output) if output.strip() else {}

    deployment = Cloud(root, folder_id=folder, yc=yc)
    try:
        if args.action == "destroy":
            name = f"itmo-{deployment.state['owner']}"
            network = deployment.state["resources"].get("network", {})
            if network.get("managed", True):
                print(f"Будут удалены ВМ, диск с БД и сеть проекта {name}.")
            else:
                print(
                    "Будут удалены ВМ, диск с БД, подсеть и группа безопасности "
                    "проекта. Общая сеть default останется."
                )
            confirmed = input(f"Для подтверждения введите {name}: ").strip() == name
            deployment.destroy(confirmed=confirmed)
        elif args.action == "access":
            deployment.access(args.ssh_cidr)
        elif args.action == "up":
            print("Перед деплоем остановите локальный бот с этим же токеном.", flush=True)
            instance = deployment.provision(args)
            if instance.get("status") == "STOPPED":
                yc(["compute", "instance", "start", "--id", instance["id"]])
            deployment.deploy(deployment.instance(), settings)
        elif args.action in {"start", "stop"}:
            instance = deployment.instance()
            desired = "RUNNING" if args.action == "start" else "STOPPED"
            if instance.get("status") != desired:
                yc(["compute", "instance", args.action, "--id", instance["id"]])
            if args.action == "start":
                instance = deployment.instance()
                deployment.wait_ssh(instance)
                print(
                    deployment.ssh(
                        instance, f"{COMPOSE} up -d --wait --wait-timeout 180", timeout=240
                    )
                )
            print(
                "ВМ запущена."
                if args.action == "start"
                else "ВМ остановлена. Диск продолжает тарифицироваться."
            )
        else:
            instance = deployment.instance()
            print(f"ВМ: {instance['id']}; состояние: {instance.get('status', 'UNKNOWN')}")
            interfaces = instance.get("network_interfaces", [])
            if interfaces:
                address = interfaces[0].get("primary_v4_address", {}).get("one_to_one_nat", {})
                if address.get("address"):
                    print(f"Публичный IPv4: {address['address']}")
            if instance.get("status") == "RUNNING":
                command = "ps" if args.action == "status" else "logs --tail 100 bot db"
                output = deployment.ssh(instance, f"{COMPOSE} {command}")
                # Логи приложения уже проходят фильтр; дополнительная защита для вывода CLI.
                print(output)
    except BaseException:
        print(
            "Операция не завершена. Состояние сохранено в .deploy/state.json; "
            "ресурсы могут тарифицироваться. Повторите команду либо выполните deploy destroy."
        )
        raise
    return 0
