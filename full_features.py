"""Content catalogue and instance import operations for Glow Craft Launcher."""

from __future__ import annotations

import hashlib
import base64
import json
import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import minecraft_launcher_lib as mll
import requests

import launcher_core as core

API = "https://api.modrinth.com/v2"
KIND_MAP = {"mod": "mod", "shader": "shader", "resource": "resourcepack", "pack": "modpack"}
FOLDER_MAP = {"mod": "mods", "shader": "shaderpacks", "resource": "resourcepacks"}
FOOLS = re.compile(r"(?:^|[-_])(?:\d{2}w14|\d{2}w13|3d-shareware|oneblockatatime)", re.I)


def version_groups() -> dict[str, list[str]]:
    groups = {"release": [], "snapshot": [], "fools": []}
    try:
        entries = mll.utils.get_version_list()
    except requests.RequestException as error:
        raise RuntimeError("无法读取 Minecraft 官方版本列表，请检查网络连接后重试。") from error
    for entry in entries:
        identifier = str(entry.get("id", ""))
        if not identifier:
            continue
        if FOOLS.search(identifier):
            groups["fools"].append(identifier)
        elif entry.get("type") == "release":
            groups["release"].append(identifier)
        elif entry.get("type") == "snapshot":
            groups["snapshot"].append(identifier)
    return groups


def _get(path: str, params: dict | None = None):
    response = requests.get(API + path, params=params,
                            headers={"User-Agent": core.USER_AGENT}, timeout=25)
    response.raise_for_status()
    return response.json()


@lru_cache(maxsize=512)
def _icon_data(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "cdn.modrinth.com":
        return ""
    try:
        response = requests.get(url, timeout=8, headers={"User-Agent": core.USER_AGENT})
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";")[0]
        if content_type not in {"image/png", "image/jpeg", "image/webp", "image/svg+xml"}:
            return ""
        if len(response.content) > 512_000:
            return ""
        return f"data:{content_type};base64," + base64.b64encode(response.content).decode()
    except requests.RequestException:
        return ""


def search_projects(query: str, kind: str, instance: dict | None,
                    offset: int = 0) -> dict:
    if kind not in KIND_MAP:
        raise ValueError("不支持的内容分类")
    facets = [[f"project_type:{KIND_MAP[kind]}"]]
    if instance and kind != "pack":
        facets.append([f"versions:{instance['game_version']}"])
        if kind == "mod":
            loader = instance["loader"]
            if loader == "vanilla":
                return {"hits": [], "total_hits": 0, "offset": 0}
            facets.append([f"categories:{loader}"])
    data = _get("/search", {"query": query.strip(), "facets": json.dumps(facets),
                            "limit": 30, "offset": max(0, min(offset, 990)),
                            "index": "relevance" if query.strip() else "downloads"})
    hits = [{"id": item.get("project_id"), "title": item.get("title", ""),
                      "description": item.get("description", ""),
                      "icon_url": item.get("icon_url", ""),
                      "author": item.get("author", ""),
                      "downloads": item.get("downloads", 0)}
                     for item in data.get("hits", [])]
    with ThreadPoolExecutor(max_workers=8) as pool:
        icons = list(pool.map(_icon_data, [item["icon_url"] for item in hits]))
    for item, icon in zip(hits, icons):
        item["icon_data"] = icon
    return {"hits": hits,
            "total_hits": data.get("total_hits", 0), "offset": data.get("offset", offset)}


@lru_cache(maxsize=256)
def _project_title(project_id: str) -> str:
    try:
        return _get(f"/project/{quote(project_id)}").get("title", project_id)
    except requests.RequestException:
        return project_id


def project_releases(project_id: str, kind: str, instance: dict | None) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", project_id):
        raise ValueError("项目 ID 无效")
    params = {"include_changelog": "false"}
    if instance and kind != "pack":
        params["game_versions"] = json.dumps([instance["game_version"]])
        if kind == "mod":
            params["loaders"] = json.dumps([instance["loader"]])
        elif kind == "resource":
            params["loaders"] = json.dumps(["minecraft"])
    versions = _get(f"/project/{quote(project_id)}/version", params)
    dependency_ids = {dep.get("project_id") for item in versions for dep in item.get("dependencies", [])
                      if dep.get("dependency_type") == "required" and dep.get("project_id")}
    dependency_ids = sorted(dependency_ids)
    with ThreadPoolExecutor(max_workers=6) as pool:
        titles = dict(zip(dependency_ids, pool.map(_project_title, dependency_ids)))
    return [{"id": item["id"], "name": item.get("name", ""),
             "version_number": item.get("version_number", ""),
             "version_type": item.get("version_type", ""),
             "game_versions": item.get("game_versions", []),
             "loaders": item.get("loaders", []),
             "dependencies": [{"project_id": dep.get("project_id"),
                                "version_id": dep.get("version_id"),
                                "type": dep.get("dependency_type"),
                                "title": titles.get(dep.get("project_id"), "所需前置")}
                               for dep in item.get("dependencies", [])]}
            for item in versions]


def _safe_relative(value: str) -> Path:
    if "\\" in value or "\x00" in value:
        raise ValueError("整合包包含不安全的文件路径")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise ValueError("整合包包含不安全的文件路径")
    return Path(*path.parts)


def _safe_zip_members(archive: zipfile.ZipFile):
    infos = archive.infolist()
    if len(infos) > 12000 or sum(item.file_size for item in infos) > 4_000_000_000:
        raise ValueError("整合包解压内容过大")
    for item in infos:
        _safe_relative(item.filename.rstrip("/"))
        if ((item.external_attr >> 16) & 0o170000) == 0o120000:
            raise ValueError("整合包不能包含符号链接")
    return infos


def _download_manifest_file(entry: dict, destination: Path) -> None:
    hashes = entry.get("hashes", {})
    algorithm = "sha512" if hashes.get("sha512") else "sha1"
    expected = hashes.get(algorithm)
    if not expected:
        raise ValueError("整合包文件缺少校验值")
    urls = entry.get("downloads", [])
    if not urls:
        raise ValueError("整合包文件没有下载地址")
    last_error = None
    for url in urls:
        if urlparse(url).scheme != "https":
            continue
        try:
            core._download_verified({"url": url, "hashes": hashes}, destination)
            return
        except (OSError, requests.RequestException, ValueError) as error:
            last_error = error
    raise RuntimeError(f"整合包文件下载失败：{destination.name}：{last_error}")


def _install_mrpack(config: dict, archive_path: Path, folder: str,
                    name_override: str, progress) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        infos = _safe_zip_members(archive)
        try:
            index = json.loads(archive.read("modrinth.index.json"))
        except KeyError as error:
            raise ValueError("不是有效的 Modrinth 整合包") from error
        if index.get("formatVersion") != 1 or index.get("game") != "minecraft":
            raise ValueError("整合包格式不受支持")
        dependencies = index.get("dependencies", {})
        mc_version = dependencies.get("minecraft", "")
        core.valid_id(mc_version)
        loaders = [(key, loader) for key, loader in (("fabric-loader", "fabric"),
                   ("quilt-loader", "quilt"), ("neoforge", "neoforge"), ("forge", "forge"))
                   if dependencies.get(key)]
        if len(loaders) > 1:
            raise ValueError("整合包声明了多个加载器")
        loader = loaders[0][1] if loaders else "vanilla"
        progress(f"正在准备 Minecraft {mc_version} · {loader}…")
        loader_version = dependencies.get(loaders[0][0]) if loaders else None
        version_id = core.install_version(config, mc_version, loader, progress,
                                          loader_version=loader_version)
        item = core.add_instance(config, version_id, mc_version, loader, folder,
                                 name_override or index.get("name", archive_path.stem))
        destination = core.game_dir(config, item["id"])
        for entry in index.get("files", []):
            if entry.get("env", {}).get("client") == "unsupported":
                continue
            relative = _safe_relative(entry.get("path", ""))
            progress(f"正在下载 {relative.name}…")
            _download_manifest_file(entry, destination / relative)
        for prefix in ("overrides/", "client-overrides/"):
            for info in infos:
                if info.is_dir() or not info.filename.startswith(prefix):
                    continue
                relative = _safe_relative(info.filename[len(prefix):])
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    while chunk := source.read(1024 * 256):
                        output.write(chunk)
        return item


def import_pack(config: dict, archive_path: str, folder: str,
                game_version: str = "", loader: str = "vanilla",
                name: str = "", progress=lambda text: None) -> dict:
    path = Path(archive_path).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in {".zip", ".mrpack"}:
        raise ValueError("请选择 ZIP 或 MRPACK 整合包")
    with zipfile.ZipFile(path) as archive:
        infos = _safe_zip_members(archive)
        if "modrinth.index.json" in archive.namelist():
            return _install_mrpack(config, path, folder, name, progress)
        if "manifest.json" in archive.namelist():
            raise ValueError("这是 CurseForge 整合包，需要平台下载授权；请导出为 MRPACK 或完整游戏文件夹 ZIP")
        if not game_version:
            raise ValueError("导入普通 ZIP 前请先选择目标游戏版本")
        core.valid_id(game_version)
        version_id = core.install_version(config, game_version, loader, progress)
        item = core.add_instance(config, version_id, game_version, loader, folder, name or path.stem)
        destination = core.game_dir(config, item["id"])
        file_parts = [PurePosixPath(info.filename).parts for info in infos if not info.is_dir()]
        roots = {parts[0] for parts in file_parts if parts}
        common_root = next(iter(roots)) if len(roots) == 1 and all(len(parts) > 1 for parts in file_parts) else ""
        for info in infos:
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename).parts
            if common_root and parts[0] == common_root:
                parts = parts[1:]
            while parts and parts[0] in {".minecraft", "minecraft", "overrides", path.stem}:
                parts = parts[1:]
            if not parts:
                continue
            relative = _safe_relative("/".join(parts))
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 256):
                    output.write(chunk)
        return item


def install_project_release(config: dict, project_id: str, release_id: str,
                            kind: str, instance_id: str, progress=lambda text: None) -> list[str]:
    if kind not in FOLDER_MAP:
        raise ValueError("请选择模组、光影或资源包")
    item = core.instance(config, instance_id)
    installed: list[str] = []
    seen: set[str] = set()

    def install(release: dict, current_kind: str):
        identifier = release["id"]
        if identifier in seen:
            return
        seen.add(identifier)
        if item["game_version"] not in release.get("game_versions", []):
            raise ValueError("发布文件与目标游戏版本不兼容")
        if current_kind == "mod" and item["loader"] not in release.get("loaders", []):
            raise ValueError("发布文件与目标加载器不兼容")
        for dependency in release.get("dependencies", []):
            if dependency.get("dependency_type") != "required":
                continue
            if dependency.get("version_id"):
                dependency_release = _get(f"/version/{quote(dependency['version_id'])}")
            elif dependency.get("project_id"):
                versions = project_releases(dependency["project_id"], "mod", item)
                if not versions:
                    raise ValueError("找不到所需前置模组的兼容文件")
                dependency_release = _get(f"/version/{quote(versions[0]['id'])}")
            else:
                raise ValueError("项目需要无法自动获取的前置文件")
            install(dependency_release, "mod")
        files = release.get("files", [])
        selected = next((file for file in files if file.get("primary")), files[0] if files else None)
        if not selected:
            raise ValueError("发布版本没有文件")
        filename = selected.get("filename", "")
        if filename != Path(filename).name or not filename.lower().endswith((".jar", ".zip")):
            raise ValueError("发布文件名无效")
        destination = core.instance_folder(config, instance_id, FOLDER_MAP[current_kind]) / filename
        progress(f"正在安装 {filename}…")
        core._download_verified(selected, destination)
        installed.append(filename)

    release = _get(f"/version/{quote(release_id)}")
    if release.get("project_id") != project_id:
        raise ValueError("发布文件不属于选中的项目")
    install(release, kind)
    return installed
