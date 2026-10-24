#!/bin/sh
set -eu
cd "$(dirname "$0")"
build_python="${GCL_PYTHON:-.venv/bin/python}"
if [ ! -x "$build_python" ]; then
  python3 -m venv .venv
fi
if ! "$build_python" -c "import PyInstaller, PySide6, minecraft_launcher_lib, keyring; from PySide6.QtWebEngineWidgets import QWebEngineView" 2>/dev/null; then
  "$build_python" -m pip install -r requirements.txt pyinstaller
fi
if ! command -v go >/dev/null 2>&1; then
  echo "需要 Go 来构建 GCL 内置的 Minecraft 联机组件 (frpc)" >&2
  exit 1
fi
mkdir -p .gcl-build
go build -trimpath -ldflags="-s -w" -o .gcl-build/frpc github.com/fatedier/frp/cmd/frpc@v0.59.0
ssl_policy=$("$build_python" -c 'import ssl; from pathlib import Path; p=Path(ssl.__file__); b=p.with_name("codex-rejected-cas.pem"); print(b if b.is_file() and "codex-rejected-cas.pem" in p.read_text() else "")')
if [ -n "$ssl_policy" ]; then
  set -- --add-data "$ssl_policy:."
else
  set --
fi
"$build_python" -m PyInstaller --noconfirm --windowed --name "Glow Craft Launcher" \
  --icon icon.icns \
  --add-data "ui.html:." --add-data "ui.js:." \
  --add-data "skinview3d.min.js:." --add-data "lucide.min.js:." \
  --add-data "THIRD_PARTY.md:." --add-data "icon.png:." --add-data "update-channel.json:." \
  --add-data "update-install.sh:." --add-data "licenses:licenses" \
  --add-binary ".gcl-build/frpc:." \
  --collect-all minecraft_launcher_lib --collect-all keyring "$@" app_web.py
app="dist/Glow Craft Launcher.app"
app_version=$("$build_python" -c 'import launcher_updater; print(launcher_updater.APP_VERSION)')
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier cn.glowcraft.launcher" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $app_version" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $app_version" "$app/Contents/Info.plist"
framework="$app/Contents/Frameworks/PySide6/Qt/lib/QtWebEngineCore.framework"
helper_path=$("$build_python" -c 'from pathlib import Path; import PySide6; print(Path(PySide6.__file__).parent / "Qt/lib/QtWebEngineCore.framework/Helpers/QtWebEngineProcess.app")')
mkdir -p "$framework/Versions/A/Helpers"
cp -R "$helper_path" "$framework/Versions/A/Helpers/"
# PyInstaller puts WebEngine data outside the framework's active A version.
# Move it into the canonical Resources location so Qt and macOS both find it.
if [ -d "$framework/Versions/Resources/Resources" ]; then
  cp -R "$framework/Versions/Resources/Resources/." "$framework/Versions/A/Resources/"
  rm -rf "dist/qt-webengine-misplaced-resources"
  mv "$framework/Versions/Resources" "dist/qt-webengine-misplaced-resources"
fi
codesign --force --sign - "$framework/Versions/A/Helpers/QtWebEngineProcess.app"
codesign --force --sign - "$framework"
codesign --force --deep --sign - "dist/Glow Craft Launcher.app"
codesign --verify --deep --strict "dist/Glow Craft Launcher.app"
echo "完成：dist/Glow Craft Launcher.app"
