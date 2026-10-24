"""Cross-platform Minecraft launcher operations. No PCL code or game files included."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import keyring
import minecraft_launcher_lib as mll
import requests

APP_NAME = "Glow Craft Launcher"
USER_AGENT = "GlowCraftLauncher/0.1 (personal desktop launcher)"
SERVICE = "blocklight-launcher-microsoft"
APP_DATA_DIR_NAME = "Blocklight Launcher"  # Keep existing settings and instances after the rename.
SAFE_ID = re.compile(r"^[A-Za-z0-9._+-]+$")


def app_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif os.sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    path = root / APP_DATA_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_dir() / "config.json"


def load_config() -> dict:
    defaults = {"minecraft_dir": mll.utils.get_minecraft_directory(),
                "client_id": "", "redirect_uri": "http://localhost:8765/callback",
                "account": None, "selected_version": "", "selected_loader": "vanilla"}
    try:
        value = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(value, dict):
            defaults.update(value)
            if defaults.get("account_mode") == "online":
                defaults["account_mode"] = "microsoft"
    except (OSError, ValueError):
        pass
    return defaults


def save_config(config: dict) -> None:
    path = config_path()
    fd, tmp = tempfile.mkstemp(prefix="config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def base_dir(config: dict) -> Path:
    return Path(config["minecraft_dir"]).expanduser().resolve()


def valid_id(value: str) -> str:
    if not value or not SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError("版本或实例名称包含不安全字符")
    return value


def game_dir(config: dict, version_id: str) -> Path:
    path = app_dir() / "instances" / valid_id(version_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def installed_versions(config: dict) -> list[str]:
    return sorted((v["id"] for v in mll.utils.get_installed_versions(base_dir(config))), reverse=True)


def available_versions() -> list[str]:
    return [v["id"] for v in mll.utils.get_version_list() if v.get("type") == "release"]


def install_version(config: dict, version: str, loader: str, progress=lambda msg: None,
                    loader_version: str | None = None) -> str:
    valid_id(version)
    target = base_dir(config)
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=target):
        pass
    callback = {"setStatus": lambda msg: progress(str(msg))}
    def retry_install(action, description):
        attempts = 3 if config.get("retry_downloads", True) else 1
        for attempt in range(1, attempts + 1):
            try:
                return action()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    ConnectionError, TimeoutError, FileNotFoundError) as error:
                try:
                    log_path = app_dir() / "install-errors.log"
                    with log_path.open("a", encoding="utf-8") as stream:
                        stream.write(f"\n[{datetime.now().isoformat()}] {description} attempt {attempt}\n")
                        stream.write(traceback.format_exc())
                except OSError:
                    pass
                if attempt == attempts:
                    raise RuntimeError(f"{description}失败。请检查网络连接与游戏目录，然后重试。详细原因：{error}\n诊断日志：{app_dir() / 'install-errors.log'}") from error
                progress(f"{description}连接中断，正在重试（{attempt}/{attempts}）…")
                time.sleep(attempt * 2)

    progress(f"正在安装 Minecraft {version}…")
    def install_vanilla():
        mll.install.install_minecraft_version(version, target, callback=callback)
        for path in (target / "versions" / version / f"{version}.json",
                     target / "versions" / version / f"{version}.jar"):
            if not path.is_file():
                raise FileNotFoundError(f"游戏文件未完整下载：{path.name}")
    retry_install(install_vanilla,
                  "游戏文件下载")
    if loader == "vanilla":
        return version
    if loader not in {"fabric", "quilt", "neoforge", "forge"}:
        raise ValueError("不支持的 Mod 加载器")
    progress(f"正在安装 {loader.title()}…")
    mod_loader = mll.mod_loader.get_mod_loader(loader)
    if not mod_loader.is_minecraft_version_supported(version):
        raise ValueError(f"{loader.title()} 暂不支持 Minecraft {version}")
    runtime_info = mll.runtime.get_version_runtime_information(version, target)
    java = None
    if runtime_info:
        java = mll.runtime.get_executable_path(runtime_info["name"], target)
    return retry_install(lambda: mod_loader.install(version, target, loader_version=loader_version,
                                                    callback=callback, java=java),
                         f"{loader.title()} 安装")


def launch(config: dict, version_id: str, account: dict, server: str = "") -> int:
    valid_id(version_id)
    if version_id not in installed_versions(config):
        raise ValueError("此版本尚未安装")
    options = {"username": account["name"], "uuid": account["uuid"],
               "token": account["access_token"], "launcherName": APP_NAME,
               "launcherVersion": "0.1", "gameDirectory": str(game_dir(config, version_id))}
    if server.strip():
        host, _, port = server.strip().partition(":")
        options["server"] = host
        if port:
            if not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError("服务器端口无效")
            options["port"] = port
    command = mll.command.get_minecraft_command(version_id, base_dir(config), options)
    return subprocess.Popen(command, cwd=game_dir(config, version_id)).pid


def list_instances(config: dict) -> list[dict]:
    """Return independent game directories, importing older launcher profiles once."""
    installed = set(installed_versions(config))
    records = config.setdefault("instances", {})
    for version_id in installed:
        if any(item.get("game_version") == version_id and item.get("loader") != "vanilla"
               for item in records.values()):
            continue
        if not any(item.get("version_id") == version_id for item in records.values()):
            details = config.get("profiles", {}).get(version_id, {})
            records[version_id] = {
                "version_id": version_id,
                "game_version": details.get("game_version", version_id),
                "loader": details.get("loader", "vanilla"),
                "folder": "First",
                "name": version_id,
            }
    return [{"id": identifier, **item} for identifier, item in records.items()
            if item.get("version_id") in installed]


def add_instance(config: dict, version_id: str, game_version: str,
                 loader: str, folder: str, name: str) -> dict:
    valid_id(version_id)
    folder = folder.strip()[:48] or "First"
    name = name.strip()[:64] or version_id
    records = config.setdefault("instances", {})
    identifier = version_id
    suffix = 2
    while identifier in records:
        identifier = f"{version_id}-{suffix}"
        suffix += 1
    records[identifier] = {"version_id": version_id, "game_version": game_version,
                           "loader": loader, "folder": folder, "name": name}
    save_config(config)
    path = game_dir(config, identifier)
    for folder_name in ("saves", "backups", "mods", "shaderpacks", "resourcepacks", "screenshots"):
        (path / folder_name).mkdir(exist_ok=True)
    return {"id": identifier, **records[identifier]}


def instance(config: dict, identifier: str) -> dict:
    valid_id(identifier)
    item = config.get("instances", {}).get(identifier)
    if not item:
        raise ValueError("找不到这个游戏实例")
    return {"id": identifier, **item}


def instance_folder(config: dict, identifier: str, folder: str = "game") -> Path:
    instance(config, identifier)
    folders = {"game": "", "saves": "saves", "backups": "backups",
               "mods": "mods", "shaderpacks": "shaderpacks",
               "resourcepacks": "resourcepacks", "screenshots": "screenshots"}
    if folder not in folders:
        raise ValueError("不支持的文件夹")
    target = game_dir(config, identifier) / folders[folder]
    target.mkdir(parents=True, exist_ok=True)
    return target


def rename_instance(config: dict, identifier: str, name: str) -> dict:
    item = instance(config, identifier)
    name = name.strip()
    if not name or len(name) > 64:
        raise ValueError("名称须为 1–64 个字符")
    config["instances"][identifier]["name"] = name
    save_config(config)
    return instance(config, identifier)


def list_mod_files(config: dict, identifier: str) -> list[dict]:
    folder = instance_folder(config, identifier, "mods")
    files = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_file() and path.name.lower().endswith((".jar", ".jar.disabled")):
            files.append({"name": path.name.removesuffix(".disabled"),
                          "enabled": path.suffix.lower() == ".jar"})
    return files


def set_mod_enabled(config: dict, identifier: str, filename: str, enabled: bool) -> list[dict]:
    if filename != Path(filename).name or not filename.lower().endswith(".jar"):
        raise ValueError("模组文件名无效")
    folder = instance_folder(config, identifier, "mods")
    source = folder / (filename + (".disabled" if enabled else ""))
    target = folder / (filename if enabled else filename + ".disabled")
    if not source.is_file():
        raise FileNotFoundError("模组文件不存在")
    if target.exists():
        raise FileExistsError("目标文件已存在，未覆盖原文件")
    source.rename(target)
    return list_mod_files(config, identifier)


def backup_saves(config: dict, identifier: str) -> str:
    source = instance_folder(config, identifier, "saves")
    if not any(source.iterdir()):
        raise ValueError("当前没有可备份的存档")
    target_dir = instance_folder(config, identifier, "backups")
    filename = datetime.now().strftime("saves-%Y%m%d-%H%M%S.zip")
    destination = target_dir / filename
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source))
    return str(destination)


def launch_instance(config: dict, identifier: str, account: dict) -> int:
    item = instance(config, identifier)
    version_id = item["version_id"]
    if version_id not in installed_versions(config):
        raise ValueError("此游戏版本尚未安装完整")
    options = {"username": account["name"], "uuid": account["uuid"],
               "token": account["access_token"], "launcherName": APP_NAME,
               "launcherVersion": "1.0", "gameDirectory": str(game_dir(config, identifier))}
    memory = int(config.get("memory_gb", 4))
    if config.get("memory_auto", True):
        try:
            if os.sys.platform == "darwin":
                total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
            else:
                total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            memory = max(2, min(8, total // (1024 ** 3) // 4))
        except (OSError, ValueError, subprocess.CalledProcessError):
            memory = 4
    options["jvmArguments"] = [f"-Xmx{max(2, min(memory, 16))}G"]
    java_path = config.get("java_path", "").strip()
    if java_path:
        if not Path(java_path).is_file():
            raise FileNotFoundError("手动指定的 Java 文件不存在")
        options["executablePath"] = java_path
    command = mll.command.get_minecraft_command(version_id, base_dir(config), options)
    return subprocess.Popen(command, cwd=game_dir(config, identifier)).pid


def offline_account(name: str) -> dict:
    import uuid
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
        raise ValueError("离线名称须为 3–16 位英文字母、数字或下划线")
    # Java UUID.nameUUIDFromBytes("OfflinePlayer:" + name), without a namespace.
    raw = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    raw[6] = (raw[6] & 0x0F) | 0x30
    raw[8] = (raw[8] & 0x3F) | 0x80
    player_id = uuid.UUID(bytes=bytes(raw)).hex
    return {"name": name, "uuid": player_id, "access_token": "0", "kind": "offline"}


def store_microsoft_account(config: dict, login_data: dict) -> dict:
    skins = login_data.get("skins") or []
    capes = login_data.get("capes") or []
    skin = next((item for item in skins if item.get("state") == "ACTIVE"), skins[0] if skins else {})
    cape = next((item for item in capes if item.get("state") == "ACTIVE"), capes[0] if capes else {})
    account = {"name": login_data["name"], "uuid": login_data["id"],
               "access_token": login_data["access_token"], "kind": "microsoft",
               "skin_url": skin.get("url", ""),
               "cape_url": cape.get("url", "")}
    keyring.set_password(SERVICE, account["uuid"], login_data["refresh_token"])
    config["account"] = {"name": account["name"], "uuid": account["uuid"], "kind": "microsoft"}
    save_config(config)
    return account


def restore_microsoft_account(config: dict) -> dict | None:
    meta = config.get("account")
    if not isinstance(meta, dict) or meta.get("kind") != "microsoft":
        return None
    token = keyring.get_password(SERVICE, meta["uuid"])
    if not token or not config.get("client_id"):
        return None
    data = mll.microsoft_account.complete_refresh(config["client_id"], None, config["redirect_uri"], token)
    return store_microsoft_account(config, data)


def fetch_skin(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.hostname not in {"textures.minecraft.net", "minecraft.net"} or parsed.scheme not in {"http", "https"}:
        raise ValueError("皮肤地址不是受信任的 Minecraft 地址")
    response = requests.get(parsed._replace(scheme="https").geturl(), timeout=20)
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise ValueError("皮肤文件过大")
    return response.content


def search_modrinth(query: str, kind: str, mc_version: str, loader: str) -> list[dict]:
    if kind not in {"mod", "shader", "resourcepack"}:
        raise ValueError("不支持的内容类型")
    facets = [[f"project_type:{kind}"]]
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    if kind == "mod" and loader != "vanilla":
        facets.append([f"categories:{loader}"])
    response = requests.get("https://api.modrinth.com/v2/search",
                            params={"query": query, "facets": json.dumps(facets), "limit": 30},
                            headers={"User-Agent": USER_AGENT}, timeout=20)
    response.raise_for_status()
    return response.json().get("hits", [])


def _get_json(url: str, params: dict | None = None):
    r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    return r.json()


def _download_verified(file: dict, destination: Path) -> None:
    url = file["url"]
    if urlparse(url).scheme != "https":
        raise ValueError("内容下载地址必须使用 HTTPS")
    hashes = file.get("hashes", {})
    expected = hashes.get("sha512") or hashes.get("sha1")
    algorithm = "sha512" if hashes.get("sha512") else "sha1"
    if not expected:
        raise ValueError("文件缺少校验值")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        digest = hashlib.new(algorithm)
        fd, tmp = tempfile.mkstemp(prefix="download-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as out, requests.get(url, headers={"User-Agent": USER_AGENT},
                                                          timeout=30, stream=True) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        out.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest().lower() != expected.lower():
                raise ValueError("下载文件校验失败")
            os.replace(tmp, destination)
            return
        except (requests.RequestException, OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def install_modrinth(config: dict, project_id: str, kind: str, game_version: str,
                     loader: str, profile_id: str, progress=lambda msg: None,
                     seen: set[str] | None = None) -> list[str]:
    if kind not in {"mod", "shader", "resourcepack"}:
        raise ValueError("不支持的内容类型")
    valid_id(game_version)
    seen = seen if seen is not None else set()
    if project_id in seen:
        return []
    seen.add(project_id)
    params = {"game_versions": json.dumps([game_version])}
    if kind in {"mod", "resourcepack"}:
        params["loaders"] = json.dumps([loader if kind == "mod" else "minecraft"])
    versions = _get_json(f"https://api.modrinth.com/v2/project/{project_id}/version", params)
    if not versions:
        raise ValueError("没有找到与游戏版本及加载器兼容的文件")
    version = next((v for v in versions if v.get("version_type") == "release"), versions[0])
    folder = {"mod": "mods", "shader": "shaderpacks", "resourcepack": "resourcepacks"}[kind]
    installed = []
    for dep in version.get("dependencies", []):
        if kind == "mod" and dep.get("dependency_type") == "required" and dep.get("project_id"):
            installed += install_modrinth(config, dep["project_id"], "mod", game_version, loader,
                                         profile_id, progress, seen)
    files = version.get("files", [])
    file = next((f for f in files if f.get("primary")), files[0] if files else None)
    if not file:
        raise ValueError("项目版本没有可下载文件")
    filename = Path(file["filename"]).name
    if filename != file["filename"] or not filename.lower().endswith((".jar", ".zip")):
        raise ValueError("内容文件名无效")
    destination = game_dir(config, profile_id) / folder / filename
    progress(f"正在安装 {filename}…")
    _download_verified(file, destination)
    installed.append(filename)
    return installed
