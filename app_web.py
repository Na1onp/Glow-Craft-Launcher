"""Glow Craft Launcher desktop host for the approved three-page UI."""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import minecraft_launcher_lib as mll
from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QLineEdit, QMainWindow, QMessageBox

import full_features as features
import launcher_core as core
import launcher_updater as updater
from sakura_frp import SakuraFrp


class Job(QThread):
    message = Signal(object)
    done = Signal(str)

    def __init__(self, name, action):
        super().__init__()
        self.name, self.action = name, action

    def run(self):
        try:
            value = self.action(lambda msg: self.message.emit(msg))
            self.done.emit(json.dumps({"job": self.name, "ok": True, "data": value}, ensure_ascii=False))
        except Exception as exc:
            logging.exception("Job %s failed", self.name)
            self.done.emit(json.dumps({"job": self.name, "ok": False, "error": str(exc)}, ensure_ascii=False))


class Bridge(QObject):
    notification = Signal(str)

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.config = core.load_config()
        self.account = None
        self.job = None
        self.pending_jobs = []
        self.events = []
        self.update_release = None
        self.staged_update = None
        self._skin_cache_key = None
        self._skin_cache_value = ""
        self.room_process = None
        self.room_config_path = None
        self.room_info = None
        self.java_status = "未发现系统 Java；安装游戏时会自动准备"
        java_executable = shutil.which("java")
        if java_executable:
            try:
                check = subprocess.run([java_executable, "-version"], capture_output=True,
                                       text=True, timeout=4)
                if check.returncode == 0:
                    self.java_status = (check.stderr or check.stdout).splitlines()[0]
            except (OSError, subprocess.TimeoutExpired):
                pass

    def emit(self, name, data=None):
        packet = {"event": name, "data": data}
        self.events.append(packet)
        self.notification.emit(json.dumps(packet, ensure_ascii=False))

    def state(self):
        instances = core.list_instances(self.config)
        selected = self.config.get("selected_instance")
        if selected not in {item["id"] for item in instances}:
            selected = instances[0]["id"] if instances else ""
            self.config["selected_instance"] = selected
            core.save_config(self.config)
        meta = self.config.get("account") or {}
        return {"instances": instances, "selected": selected,
                "folders": sorted(set(self.config.get("folders", ["First"])) |
                                  {item["folder"] for item in instances}),
                "config": {key: self.config.get(key) for key in (
                    "minecraft_dir", "client_id", "redirect_uri", "offline_name",
                    "account_mode", "memory_auto", "memory_gb", "java_path",
                    "motion", "motion_speed", "scale", "window_mode", "retry_downloads", "update_feed_url")},
                "data_dir": str(core.app_dir()),
                "platform": "macOS" if sys.platform == "darwin" else "Windows" if os.name == "nt" else sys.platform,
                "account": {"name": self.account.get("name"), "kind": self.account.get("kind")}
                           if self.account else meta,
                "java": self.java_status,
                "app_version": updater.APP_VERSION,
                "update_feed_configured": bool(updater.feed_url(self.config, Path(__file__).parent)),
                "sakura_connected": bool(self._sakura_token()),
                "room": self.room_info,
                "skin": self.skin_data(), "cape": self.cape_data()}

    @staticmethod
    def _sakura_token():
        try:
            import keyring
            return keyring.get_password("glow-craft-launcher-sakurafrp", "access-token") or ""
        except Exception:
            logging.exception("SakuraFrp token storage is unavailable")
            return ""

    def _bind_sakura(self):
        QDesktopServices.openUrl(QUrl("https://www.natfrp.com/"))
        token, ok = QInputDialog.getText(
            self.window, "绑定 SakuraFrp", "请在 SakuraFrp 用户信息页复制访问密钥并粘贴到这里。\nGCL 会将密钥保存在系统钥匙串中。",
            QLineEdit.EchoMode.Password)
        if not ok:
            return self.state()
        token = token.strip()
        SakuraFrp(token).request("/user/info")
        import keyring
        keyring.set_password("glow-craft-launcher-sakurafrp", "access-token", token)
        return self.state()

    @staticmethod
    def _frpc_path():
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        path = root / ("frpc.exe" if os.name == "nt" else "frpc")
        if not path.is_file():
            raise FileNotFoundError("GCL 安装包缺少内置联机组件 frpc，请安装包含联机功能的版本")
        return path

    def _host_room(self, port, progress):
        token = self._sakura_token()
        if not token:
            raise ValueError("请先绑定 SakuraFrp 访问密钥")
        if self.room_process and self.room_process.poll() is None:
            raise ValueError("联机房间已经开启；请先关闭当前房间")
        frpc_path = self._frpc_path()
        client = SakuraFrp(token)
        room = client.create_minecraft_room(int(port), progress)
        config_path = core.app_dir() / "sakurafrp-room.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(room["config"], encoding="utf-8")
        try:
            if os.name != "nt":
                config_path.chmod(0o600)
            log_path = core.app_dir() / "sakurafrp-room.log"
            with log_path.open("wb") as log_file:
                self.room_process = subprocess.Popen(
                    [str(frpc_path), "-c", str(config_path)], cwd=str(core.app_dir()),
                    stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            time.sleep(1.2)
            if self.room_process.poll() is not None:
                detail = log_path.read_text(encoding="utf-8", errors="replace")[-1800:]
                raise RuntimeError("穿透组件未能启动：" + (detail.strip() or "请检查联机日志"))
        except Exception:
            config_path.unlink(missing_ok=True)
            try:
                client.delete_tunnel(room["id"])
            except Exception:
                logging.exception("Could not remove failed SakuraFrp tunnel")
            self.room_process = None
            raise
        self.room_config_path = config_path
        self.room_info = {"endpoint": room["endpoint"], "node": room["node"], "id": room["id"]}
        return {"endpoint": room["endpoint"], "node": room["node"], "warning": room["warning"]}

    def _stop_room(self):
        process, self.room_process = self.room_process, None
        room, self.room_info = self.room_info, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if self.room_config_path:
            self.room_config_path.unlink(missing_ok=True)
            self.room_config_path = None
        if room:
            token = self._sakura_token()
            if token:
                SakuraFrp(token).delete_tunnel(room["id"])
        return self.state()

    def cape_data(self):
        if self.config.get("account_mode") != "microsoft":
            return ""
        path = core.app_dir() / "account-cape.png"
        if not path.is_file():
            return ""
        try:
            payload = path.read_bytes()
            if len(payload) <= 2_000_000:
                return "data:image/png;base64," + base64.b64encode(payload).decode()
        except OSError:
            pass
        return ""

    def skin_data(self):
        if self.config.get("account_mode") == "microsoft":
            path = core.app_dir() / "account-skin.png"
        else:
            path = self.config.get("offline_skin")
        path = Path(path) if path else None
        key = (str(path), path.stat().st_mtime_ns) if path and path.is_file() else None
        if key == self._skin_cache_key:
            return self._skin_cache_value
        self._skin_cache_key = key
        self._skin_cache_value = ""
        if path and Path(path).is_file():
            try:
                payload = Path(path).read_bytes()
                if len(payload) <= 2_000_000:
                    self._skin_cache_value = "data:image/png;base64," + base64.b64encode(payload).decode()
            except OSError:
                pass
        return self._skin_cache_value

    def start(self, name, action):
        if self.job and self.job.isRunning():
            if name in {"search", "releases"}:
                self.pending_jobs = [(n, fn) for n, fn in self.pending_jobs if n != name]
            self.pending_jobs.append((name, action))
            return {"queued": name}
        job = Job(name, action)
        self.job = job
        job.message.connect(lambda msg: self.emit("progress", msg))
        job.done.connect(self._job_done)
        job.start()
        return {"started": name}

    def _job_done(self, payload):
        result = json.loads(payload)
        if result.get("ok") and result.get("job") == "download_update":
            self.staged_update = result.get("data")
        self.emit("job", result)
        self.emit("state", self.state())
        self.job = None
        if self.pending_jobs:
            name, action = self.pending_jobs.pop(0)
            self.start(name, action)

    def _save(self, patch):
        allowed = {"client_id", "redirect_uri", "offline_name", "account_mode",
                   "memory_auto", "memory_gb", "java_path", "motion", "motion_speed",
                   "scale", "window_mode", "selected_instance", "retry_downloads", "update_feed_url"}
        if "update_feed_url" in patch and str(patch["update_feed_url"]).strip():
            updater.https_url(str(patch["update_feed_url"]).strip())
        for key, value in patch.items():
            if key in allowed:
                self.config[key] = value
        core.save_config(self.config)
        return self.state()

    def _login(self, progress):
        client_id = str(self.config.get("client_id") or "").strip()
        redirect = str(self.config.get("redirect_uri") or "http://localhost:8765/callback").strip()
        parsed = urlparse(redirect)
        if not client_id or parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or not parsed.port:
            raise ValueError("请在设置中填写 Microsoft 应用 Client ID；回调地址须为本机 HTTP 地址")
        url, state, verifier = mll.microsoft_account.get_secure_login_data(client_id, redirect)
        received = {}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received["url"] = f"http://{self.headers.get('Host')}{self.path}"
                body = "登录已收到，可以返回 Glow Craft Launcher。".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = HTTPServer((parsed.hostname, parsed.port), Handler)
        server.timeout = 180
        try:
            progress("请在浏览器中完成 Microsoft 登录…")
            webbrowser.open(url)
            server.handle_request()
        finally:
            server.server_close()
        if "url" not in received:
            raise TimeoutError("Microsoft 登录已超时")
        code = mll.microsoft_account.parse_auth_code_url(received["url"], state)
        data = mll.microsoft_account.complete_login(client_id, None, redirect, code, verifier)
        self.account = core.store_microsoft_account(self.config, data)
        if self.account.get("skin_url"):
            try:
                (core.app_dir() / "account-skin.png").write_bytes(core.fetch_skin(self.account["skin_url"]))
            except Exception:
                logging.exception("Could not cache Microsoft skin")
        cape_path = core.app_dir() / "account-cape.png"
        cape_path.unlink(missing_ok=True)
        if self.account.get("cape_url"):
            try:
                cape_path.write_bytes(core.fetch_skin(self.account["cape_url"]))
            except Exception:
                logging.exception("Could not cache Microsoft cape")
        return {"name": self.account["name"]}

    @Slot(str, result=str)
    def request(self, raw):
        try:
            req = json.loads(raw)
            op = req.get("op")
            args = req.get("args") or {}
            result = self._request(op, args)
            return json.dumps({"ok": True, "data": result}, ensure_ascii=False)
        except Exception as exc:
            logging.exception("Request failed")
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def _request(self, op, args):
        if op == "state":
            return self.state()
        if op == "poll":
            events = self.events[:]
            self.events.clear()
            return events
        if op == "save":
            return self._save(args)
        if op == "bind_sakura":
            return self._bind_sakura()
        if op == "ask_lan_port":
            port, ok = QInputDialog.getInt(
                self.window, "开放局域网端口", "先在 Minecraft 中选择“对局域网开放”，再输入聊天栏显示的端口：",
                25565, 1, 65535)
            return {"cancelled": not ok, "port": port}
        if op == "host_room":
            if not self._sakura_token():
                QMessageBox.information(self.window, "绑定 SakuraFrp", "首次创建房间需要绑定自己的 SakuraFrp 访问密钥。接下来会打开官网。")
                self._bind_sakura()
            if not self._sakura_token():
                raise ValueError("尚未绑定 SakuraFrp 账号")
            return self.start("host_room", lambda progress: self._host_room(args.get("port"), progress))
        if op == "stop_room":
            return self.start("stop_room", lambda _progress: self._stop_room())
        if op == "ask_room_address":
            address, ok = QInputDialog.getText(self.window, "加入好友房间", "输入房主分享的地址（域名或 IP:端口）：")
            return {"cancelled": not ok, "address": address.strip()}
        if op == "copy_text":
            QApplication.clipboard().setText(str(args.get("text") or ""))
            return True
        if op == "new_folder":
            value, ok = QInputDialog.getText(self.window, "新建游戏目录", "目录名称")
            if not ok:
                return {"cancelled": True}
            value = value.strip()
            if not value or len(value) > 48 or any(char in value for char in "/\\:\x00"):
                raise ValueError("目录名称须为 1–48 个字符，不能包含路径符号")
            folders = self.config.setdefault("folders", ["First"])
            if value not in folders:
                folders.append(value)
                core.save_config(self.config)
            return {"folder": value, "state": self.state()}
        if op == "check_update":
            def action(progress):
                release = updater.check(self.config, Path(__file__).parent)
                self.update_release = release
                return release
            return self.start("check_update", action)
        if op == "download_update":
            if not self.update_release or self.update_release.get("status") != "available":
                raise ValueError("请先检查更新")
            return self.start("download_update", lambda progress: updater.download_and_stage(
                self.update_release, core.app_dir(), progress))
        if op == "install_update":
            if not self.staged_update:
                raise ValueError("请先下载更新")
            helper_name = "update-install.ps1" if sys.platform == "win32" else "update-install.sh"
            updater.install_after_quit(self.staged_update, updater.current_app(),
                                       Path(__file__).with_name(helper_name), core.app_dir())
            QTimer.singleShot(100, QApplication.instance().quit)
            return True
        if op == "versions":
            return self.start("versions", lambda progress: features.version_groups())
        if op == "search":
            item = core.instance(self.config, args["instance"]) if args.get("instance") else None
            return self.start("search", lambda progress: features.search_projects(
                args.get("query", ""), args["kind"], item, int(args.get("offset", 0))))
        if op == "releases":
            item = core.instance(self.config, args["instance"]) if args.get("instance") else None
            return self.start("releases", lambda progress: features.project_releases(
                args["project"], args["kind"], item))
        if op == "mod_icons":
            item = core.instance(self.config, args["instance"])
            return self.start("mod_icons", lambda progress: features.mod_file_icons(
                args.get("files", []), item))
        if op == "plan_mod_cart":
            item = core.instance(self.config, args["instance"])
            roots = args.get("roots", [])
            return self.start("plan_mod_cart", lambda progress: features.resolve_mod_cart(item, roots))
        if op == "install_mod_cart":
            return self.start("install_mod_cart", lambda progress: features.install_mod_cart(
                self.config, args["instance"], args.get("roots", []), progress))
        if op == "install_game":
            version, loader = args["version"], args["loader"]
            folder = str(args.get("folder") or "First")
            def action(progress):
                version_id = core.install_version(self.config, version, loader, progress)
                item = core.add_instance(self.config, version_id, version, loader, folder,
                                         str(args.get("name") or f"Minecraft {version} · {loader.title()}"))
                self.config["selected_instance"] = item["id"]
                core.save_config(self.config)
                return item
            return self.start("install_game", action)
        if op == "install_content":
            return self.start("install_content", lambda progress: features.install_project_release(
                self.config, args["project"], args["release"], args["kind"],
                args["instance"], progress))
        if op == "import_pack":
            path, _ = QFileDialog.getOpenFileName(self.window, "导入整合包", "", "整合包 (*.mrpack *.zip)")
            if not path:
                return {"cancelled": True}
            return self.start("import_pack", lambda progress: features.import_pack(
                self.config, path, args.get("folder") or "First", args.get("version") or "",
                args.get("loader") or "vanilla", "", progress))
        if op == "install_pack":
            def action(progress):
                release = features._get(f"/version/{args['release']}")
                if release.get("project_id") != args["project"]:
                    raise ValueError("发布文件与整合包不匹配")
                files = release.get("files") or []
                file = next((f for f in files if f.get("primary")), files[0] if files else None)
                if not file or not file.get("filename", "").lower().endswith(".mrpack"):
                    raise ValueError("此发布版本没有 MRPACK 文件")
                with tempfile.TemporaryDirectory() as temp:
                    target = Path(temp) / "pack.mrpack"
                    progress("正在下载整合包…")
                    core._download_verified(file, target)
                    return features.import_pack(self.config, str(target), args.get("folder") or "First", progress=progress)
            return self.start("install_pack", action)
        if op == "rename":
            return core.rename_instance(self.config, args["instance"], args["name"])
        if op == "mods":
            return core.list_mod_files(self.config, args["instance"])
        if op == "toggle_mod":
            return core.set_mod_enabled(self.config, args["instance"], args["filename"], bool(args["enabled"]))
        if op == "open_folder":
            path = core.instance_folder(self.config, args["instance"], args["folder"])
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return str(path)
        if op == "backup":
            return core.backup_saves(self.config, args["instance"])
        if op == "choose_game_dir":
            path = QFileDialog.getExistingDirectory(self.window, "选择游戏文件目录", self.config["minecraft_dir"])
            if path:
                self.config["minecraft_dir"] = path
                core.save_config(self.config)
            return self.state()
        if op == "choose_java":
            path, _ = QFileDialog.getOpenFileName(self.window, "选择 Java 可执行文件")
            if path:
                self.config["java_path"] = path
                core.save_config(self.config)
            return self.state()
        if op == "choose_skin":
            path, _ = QFileDialog.getOpenFileName(self.window, "导入本地皮肤", "", "PNG 图片 (*.png)")
            if path:
                from PySide6.QtGui import QImage
                image = QImage(path)
                if image.isNull() or image.width() != 64 or image.height() not in (32, 64):
                    raise ValueError("请选择 64×64 或旧版 64×32 Minecraft 皮肤 PNG")
                self.config["offline_skin"] = path
                core.save_config(self.config)
            return self.state()
        if op == "login":
            return self.start("login", self._login)
        if op == "logout":
            import keyring
            meta = self.config.get("account") or {}
            if meta.get("uuid"):
                try:
                    keyring.delete_password(core.SERVICE, meta["uuid"])
                except keyring.errors.PasswordDeleteError:
                    pass
            self.config["account"] = None
            self.account = None
            (core.app_dir() / "account-skin.png").unlink(missing_ok=True)
            (core.app_dir() / "account-cape.png").unlink(missing_ok=True)
            core.save_config(self.config)
            return self.state()
        if op == "launch":
            name = args.get("name") or self.config.get("offline_name") or "Player"
            mode = self.config.get("account_mode") or "offline"
            def action(progress):
                if mode == "microsoft":
                    progress("正在验证 Microsoft 账户…")
                    account = core.restore_microsoft_account(self.config)
                    if not account:
                        raise ValueError("请先完成 Microsoft 正版登录")
                    self.account = account
                else:
                    account = core.offline_account(name)
                progress("正在启动游戏…")
                return core.launch_instance(self.config, args["instance"], account,
                                            str(args.get("server_address") or ""))
            return self.start("launch", action)
        if op == "open_logs":
            path = core.app_dir() / "launcher.log"
            path.touch(exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return str(path)
        if op == "open_app_data":
            path = core.app_dir()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return str(path)
        if op == "open_profile":
            QDesktopServices.openUrl(QUrl("https://www.minecraft.net/en-us/msaprofile/mygames/editskin"))
            return True
        if op == "open_license":
            path = Path(__file__).with_name("THIRD_PARTY.md")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return str(path)
        if op == "close":
            try:
                self._stop_room()
            except Exception:
                logging.exception("Could not cleanly stop room tunnel")
            self.window.close()
            return True
        raise ValueError("未知操作")


class AppPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
        logging.warning("UI JavaScript %s:%s: %s", source, line, message)


class LauncherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Glow Craft Launcher")
        self.resize(1060, 720)
        self.setMinimumSize(850, 620)
        self.web = QWebEngineView(self)
        self.web.setPage(AppPage(self.web))
        self.setCentralWidget(self.web)
        self.bridge = Bridge(self)
        self.channel = QWebChannel(self.web.page())
        self.channel.registerObject("launcher", self.bridge)
        self.web.page().setWebChannel(self.channel)
        self.web.load(QUrl.fromLocalFile(str(Path(__file__).with_name("ui.html").resolve())))


def main():
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        frameworks = Path(sys.executable).resolve().parent.parent / "Frameworks"
        resources = (frameworks / "PySide6/Qt/lib/QtWebEngineCore.framework"
                     / "Versions/A/Resources")
        if resources.is_dir():
            os.environ.setdefault("QTWEBENGINE_RESOURCES_PATH", str(resources))
            os.environ.setdefault("QTWEBENGINE_LOCALES_PATH",
                                  str(resources / "qtwebengine_locales"))
    log = core.app_dir() / "launcher.log"
    logging.basicConfig(filename=log, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("Glow Craft Launcher")
    window = LauncherWindow()
    window.show()
    QTimer.singleShot(4000, lambda: window.bridge._request("check_update", {}))
    timer = QTimer(window)
    timer.timeout.connect(lambda: window.bridge._request("check_update", {}))
    timer.start(6 * 60 * 60 * 1000)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
