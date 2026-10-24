"""Print an update.json for a released macOS application ZIP."""
import hashlib
import json
import plistlib
import sys
import zipfile
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        raise SystemExit("用法: python make-update-manifest.py 安装包.zip https://下载地址.zip")
    archive = Path(sys.argv[1])
    url = sys.argv[2]
    if not url.startswith("https://"):
        raise SystemExit("下载地址必须使用 HTTPS")
    with zipfile.ZipFile(archive) as zf:
        with zf.open("Glow Craft Launcher.app/Contents/Info.plist") as stream:
            info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != "cn.glowcraft.launcher":
        raise SystemExit("应用标识不符")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    print(json.dumps({"schema": 1, "version": info["CFBundleShortVersionString"], "notes": "Glow Craft Launcher 更新", "platforms": {"macos-arm64": {"url": url, "sha256": digest.hexdigest(), "size": archive.stat().st_size}}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
