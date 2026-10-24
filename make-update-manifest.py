"""Print an update.json for platform release archives."""
import hashlib
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 4 or (len(sys.argv) - 2) % 2:
        raise SystemExit("用法: python make-update-manifest.py 版本 平台 安装包.zip [平台 安装包.zip ...]")
    version = sys.argv[1]
    platforms = {}
    for key, filename in zip(sys.argv[2::2], sys.argv[3::2]):
        archive = Path(filename)
        if key not in {"macos-arm64", "macos-x86_64", "windows-x86_64"} or not archive.is_file():
            raise SystemExit(f"无效的平台或安装包: {key} {filename}")
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        platforms[key] = {"url": f"https://github.com/Na1onp/Glow-Craft-Launcher/releases/download/v{version}/{archive.name}",
                          "sha256": digest.hexdigest(), "size": archive.stat().st_size}
    print(json.dumps({"schema": 1, "version": version, "notes": "Glow Craft Launcher 更新", "platforms": platforms}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
