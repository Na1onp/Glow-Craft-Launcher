"""Small SakuraFrp API client used by GCL's optional Minecraft room feature."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

API = "https://api.natfrp.com/v4"
FRPC_VERSION = "0.59.0"


class SakuraFrpError(RuntimeError):
    pass


class SakuraFrp:
    def __init__(self, token: str):
        token = token.strip()
        if not token or len(token) > 4096:
            raise ValueError("请先绑定 SakuraFrp 访问密钥")
        self.token = token

    def request(self, path: str, payload: dict | None = None, *, text: bool = False):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            API + path, data=data,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json, text/plain",
                     **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read(1_000_001)
        except urllib.error.HTTPError as exc:
            body = exc.read(128_001)
            try:
                message = json.loads(body).get("msg", "")
            except (ValueError, AttributeError):
                message = ""
            raise SakuraFrpError(message or f"SakuraFrp API 返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SakuraFrpError(f"连接 SakuraFrp 失败：{exc}") from exc
        if len(body) > 1_000_000:
            raise SakuraFrpError("SakuraFrp 返回内容过大")
        if text:
            return body.decode("utf-8")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SakuraFrpError("SakuraFrp 返回了无法识别的数据") from exc
        if isinstance(result, dict) and result.get("code", 200) >= 400:
            raise SakuraFrpError(result.get("msg") or "SakuraFrp 请求失败")
        return result

    def create_minecraft_room(self, port: int, progress=lambda _message: None) -> dict:
        if isinstance(port, bool) or not 1 <= int(port) <= 65535:
            raise ValueError("局域网端口应为 1–65535 的数字")
        port = int(port)
        progress("正在读取可用穿透节点…")
        raw_nodes = self.request("/nodes")
        nodes = []
        if isinstance(raw_nodes, dict):
            for key, node in raw_nodes.items():
                try:
                    node_id = int(key)
                except (TypeError, ValueError):
                    node_id = node.get("id") if isinstance(node, dict) else None
                if not isinstance(node, dict) or not node_id:
                    continue
                flag = int(node.get("flag", 0))
                if flag & (1 << 2) and not flag & (1 << 9) and not flag & (1 << 6):
                    nodes.append((node_id, node))
        if not nodes:
            raise SakuraFrpError("当前账号没有可用的穿透节点，请检查 SakuraFrp 账号与节点状态")
        # Use a currently available node; users can choose/change regions in SakuraFrp later.
        node_id, node = nodes[0]
        progress(f"正在创建 Minecraft TCP 隧道（{node.get('name') or '可用节点'}）…")
        created = self.request("/tunnels", {
            "name": f"GCL-Minecraft-{int(time.time())}",
            "type": "tcp", "node": node_id, "local_ip": "127.0.0.1",
            "local_port": port, "note": "Glow Craft Launcher Minecraft LAN room"})
        tunnel_id = created.get("id") if isinstance(created, dict) else None
        if not tunnel_id:
            raise SakuraFrpError("隧道创建成功响应缺少隧道 ID")
        try:
            progress("正在获取隧道连接配置…")
            config = self.request("/tunnel/config", {
                "query": str(tunnel_id), "frpc": FRPC_VERSION}, text=True)
            remote = str(created.get("remote") or "").strip()
            address = str(node.get("host") or "").strip()
            if not remote or not address:
                tunnels = self.request("/tunnels")
                found = next((item for item in tunnels if str(item.get("id")) == str(tunnel_id)), {})
                remote = str(found.get("remote") or remote)
            if not remote or not address:
                raise SakuraFrpError("隧道已创建，但 API 没有返回可用的连接地址")
            endpoint = remote if ":" in remote else f"{address}:{remote}"
            return {"id": int(tunnel_id), "endpoint": endpoint, "config": config,
                    "node": str(node.get("name") or ""),
                    "warning": str(created.get("important_warning_for_ai") or "")}
        except Exception:
            try:
                self.request("/tunnel/delete", {"ids": str(tunnel_id)})
            except Exception:
                pass
            raise

    def delete_tunnel(self, tunnel_id: int):
        return self.request("/tunnel/delete", {"ids": str(int(tunnel_id))})
