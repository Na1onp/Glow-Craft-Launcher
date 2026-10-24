"""Verified, opt-in macOS application updates from an HTTPS JSON feed."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

APP_VERSION = "0.7.0"
BUNDLE_ID = "cn.glowcraft.launcher"
APP_NAME = "Glow Craft Launcher.app"
MAX_ARCHIVE = 900 * 1024 * 1024
MAX_UNPACKED = 2 * 1024 * 1024 * 1024


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value):
        raise ValueError("更新版本号格式无效")
    return tuple(int(part) for part in value.split("."))


def feed_url(config: dict, resource_dir: Path) -> str:
    override = str(config.get("update_feed_url") or "").strip()
    if override:
        return override
    try:
        return str(json.loads((resource_dir / "update-channel.json").read_text(encoding="utf-8")).get("feed_url") or "").strip()
    except (OSError, ValueError, TypeError):
        return ""


def https_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("更新地址必须是有效的 HTTPS 地址")
    return url


def _read_json(url: str) -> dict:
    request = urllib.request.Request(https_url(url), headers={"User-Agent": f"GlowCraftLauncher/{APP_VERSION}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        if hasattr(response, "geturl"):
            https_url(response.geturl())
        if int(response.headers.get("Content-Length", "0")) > 128_000:
            raise ValueError("更新信息过大")
        raw = response.read(128_001)
    if len(raw) > 128_000:
        raise ValueError("更新信息过大")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("更新信息格式无效")
    return data


def check(config: dict, resource_dir: Path) -> dict:
    url = feed_url(config, resource_dir)
    if not url:
        return {"status": "unconfigured", "current": APP_VERSION, "message": "更新源尚未配置；发布地址确定后即可在这里一键更新。"}
    try:
        data = _read_json(url)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"status": "not_published", "current": APP_VERSION,
                    "message": "更新源尚未发布；首个 GitHub Release 发布后即可检查更新。"}
        raise
    if data.get("schema") != 1:
        raise ValueError("不支持的更新信息格式")
    version = str(data.get("version") or "")
    newer = version_tuple(version) > version_tuple(APP_VERSION)
    asset = (data.get("platforms") or {}).get("macos-arm64")
    if newer:
        if not isinstance(asset, dict):
            raise ValueError("新版本没有 macOS Apple 芯片安装包")
        https_url(str(asset.get("url") or ""))
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(asset.get("sha256") or "")):
            raise ValueError("更新文件缺少有效的 SHA-256 校验值")
        size = int(asset.get("size") or 0)
        if size < 1 or size > MAX_ARCHIVE:
            raise ValueError("更新文件大小无效")
    return {"status": "available" if newer else "current", "current": APP_VERSION,
            "version": version, "notes": str(data.get("notes") or "")[:4000],
            "asset": asset if newer else None}


def current_app() -> Path:
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        raise RuntimeError("应用内安装只支持打包后的 macOS 版本")
    executable = Path(sys.executable).absolute()
    for parent in executable.parents:
        if parent.suffix == ".app":
            return parent
    raise RuntimeError("未找到当前应用程序包")


def download_and_stage(release: dict, app_data: Path, progress=lambda message: None) -> dict:
    if release.get("status") != "available" or not isinstance(release.get("asset"), dict):
        raise ValueError("请先检查更新")
    version = str(release.get("version") or "")
    version_tuple(version)
    asset = release["asset"]
    url = https_url(str(asset["url"]))
    expected_hash = str(asset["sha256"]).lower()
    expected_size = int(asset["size"])
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or not 0 < expected_size <= MAX_ARCHIVE:
        raise ValueError("更新文件校验信息无效")
    root = app_data / "updates"
    root.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="stage-", dir=root))
    archive = stage_dir / "update.zip"
    try:
        progress("正在下载更新…")
        request = urllib.request.Request(url, headers={"User-Agent": f"GlowCraftLauncher/{APP_VERSION}"})
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(request, timeout=30) as response, archive.open("wb") as output:
            if hasattr(response, "geturl"):
                https_url(response.geturl())
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > MAX_ARCHIVE or size > expected_size:
                    raise ValueError("更新文件超过声明大小")
                digest.update(block)
                output.write(block)
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise ValueError("更新文件校验失败，请重新检查更新")
        progress("正在验证安装包…")
        unpacked = stage_dir / "unpacked"
        unpacked.mkdir()
        with zipfile.ZipFile(archive) as zf:
            total = 0
            symlinks = []
            for item in zf.infolist():
                name = item.filename
                parts = Path(name).parts
                if (not parts or parts[0] != APP_NAME or Path(name).is_absolute()
                        or any(part in {"..", "."} for part in parts)):
                    raise ValueError("安装包包含不安全的文件路径")
                total += item.file_size
                if total > MAX_UNPACKED:
                    raise ValueError("安装包解压后过大")
                path = unpacked / name
                if stat.S_ISLNK(item.external_attr >> 16):
                    symlinks.append(item)
                    continue
                if item.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item) as source, path.open("wb") as output:
                    shutil.copyfileobj(source, output)
                mode = (item.external_attr >> 16) & 0o777
                if mode:
                    path.chmod(mode)
            for item in symlinks:
                path = unpacked / item.filename
                target = zf.read(item).decode("utf-8")
                resolved = (path.parent / target).resolve(strict=False)
                if (Path(target).is_absolute() or not resolved.is_relative_to((unpacked / APP_NAME).resolve(strict=False))
                        or path.exists() or path.is_symlink()):
                    raise ValueError("安装包包含不安全的符号链接")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target)
        candidate = unpacked / APP_NAME
        info = candidate / "Contents/Info.plist"
        if not info.is_file():
            raise ValueError("安装包中没有 Glow Craft Launcher.app")
        with info.open("rb") as stream:
            plist = plistlib.load(stream)
        if plist.get("CFBundleIdentifier") != BUNDLE_ID or plist.get("CFBundleShortVersionString") != version:
            raise ValueError("安装包的应用标识或版本不匹配")
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate)], check=True, capture_output=True, text=True)
        archive.unlink()
        return {"version": version, "path": str(candidate), "stage_dir": str(stage_dir)}
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def install_after_quit(staged: dict, current: Path, helper: Path, app_data: Path) -> None:
    candidate = Path(staged["path"]).absolute()
    if not candidate.is_dir() or not candidate.is_relative_to(app_data / "updates"):
        raise ValueError("更新文件未准备好")
    if current.name != APP_NAME:
        raise ValueError("当前应用名称不符")
    if not os.access(current.parent, os.W_OK):
        raise PermissionError("当前位置没有写入权限。请先把应用移到用户的“应用程序”文件夹，再更新。")
    if not helper.is_file():
        raise FileNotFoundError("缺少更新安装程序")
    backup = current.with_name(".Glow Craft Launcher.previous.app")
    log = app_data / "updater.log"
    with log.open("a", encoding="utf-8") as output:
        subprocess.Popen(["/bin/sh", str(helper), str(os.getpid()), str(current), str(candidate), str(backup)],
                         stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
