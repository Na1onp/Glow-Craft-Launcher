# Glow Craft Launcher (macOS / Windows)

Glow Craft Launcher 是为 Minecraft Java 版制作的第三方启动器，macOS 与 Windows 安装包在同一个 GitHub Release 中发布。界面包含首页、下载、设置三页，并使用本地 3D 人物预览。项目没有复制 PCL 代码或打包 Minecraft 游戏文件。

## 已实现

- 从 Minecraft 官方版本清单读取正式版、快照版、愚人节版，搜索并安装原版、Fabric、Forge、NeoForge、Quilt（是否支持取决于该加载器对目标游戏版本的发布情况）。安装时下载所需 Java，连接中断可重试。
- 独立游戏实例与目录分组；首页选择目录和实例、改名、启停模组、打开存档/备份/模组/光影/资源包目录、备份存档、启动游戏。
- 离线档案与本地 PNG 皮肤；Microsoft 正版账号登录采用浏览器 PKCE，刷新令牌保存在系统密钥库。正版皮肤登录后同步到本机预览。
- 从 Modrinth 实时搜索模组、光影、资源包、整合包，按所选 Minecraft 版本和模组加载器筛选；可选择具体发布文件，下载哈希校验和必需前置。
- 模组可用常用中文名搜索（例如“苹果皮”），结果保留官方英文名并显示中文别名；安装清单会统一检查目标实例、兼容版本和必需前置。版本设置卡片显示已安装模组图标；旧模组文件会自动查询图标，打开模组管理不再触发类型错误。
- 主导航、下载分类、菜单和弹窗带有平滑过渡；版本设置与模组清单弹窗使用轻量透明度/位移动画，避免实时背景模糊与持续空转的鼠标跟随循环造成卡顿；导航短条居中显示并随页面切换平滑滑动。右下角系统标识旁显示当前应用版本。
- 导入本地 `.mrpack` 或普通 `.zip`。MRPACK 会读取其声明的游戏和加载器版本。普通 ZIP 使用当前选择的版本与加载器。CurseForge manifest 格式的 ZIP 暂不支持自动下载其中的文件。
- 游戏文件位置、Java 路径、内存、下载重试、背景动效等设置保存在本机。
- 好友联机：房主开放 Minecraft 局域网后，可从 GCL 创建 SakuraFrp TCP 隧道；GCL 安装包内置 frpc，不要求另装 SakuraFrp 图形客户端。房主需要自己的 SakuraFrp 访问密钥，密钥存入系统钥匙串。房间地址可复制，好友输入地址后由 GCL 启动 Minecraft 直连。
- 离线皮肤 PNG 只用于 GCL 里的 3D 预览；原版离线游戏不会加载它。游戏内显示需要皮肤加载器和可访问的皮肤源。Microsoft 正版登录仍需要 Microsoft 应用 Client ID。

## 运行与构建

在项目目录运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app_web.py
```

构建桌面包时还需要 Go，用于从 frp v0.59.0 官方源码构建并打包内置 `frpc`。

执行 `./build-macos.sh` 可在 `dist/` 构建 Mac 应用。当前应用为 Apple Silicon 架构；Intel Mac 需要在 Intel Mac 上重新打包。首次打开未签名应用时，macOS 可能要求在系统设置中确认。

发布包解压后，请把 `Glow Craft Launcher.app` 拖入“应用程序”文件夹再打开。它使用本地临时签名，没有 Apple 公证；若 macOS 提示无法验证开发者，请在 Finder 中按住 Control 点击应用图标并选择“打开”，或在“系统设置 → 隐私与安全性”允许打开。不要运行旧的 `preview` 压缩包。

## Microsoft 登录

需要一个允许个人 Microsoft 账户登录、并获得 Minecraft 服务访问许可的应用 Client ID，并在该应用配置中注册与设置页相同的本机回调地址，默认 `http://localhost:8765/callback`。在“设置 → 账户与皮肤”填入 Client ID 和回调地址后点击登录。我们没有该应用 ID 或用户账户，故无法代替用户完成真实账号登录验证。切勿分享密码、验证码或令牌。注册与许可要求见 [Microsoft 应用注册说明](https://learn.microsoft.com/en-us/graph/auth-register-app-v2)及 [minecraft-launcher-lib 登录说明](https://minecraft-launcher-lib.readthedocs.io/en/stable/tutorial/microsoft_login.html)。

## 数据与许可

游戏版本与共享资源默认位于 Minecraft 官方数据目录；每个实例的存档和内容位于 `~/Library/Application Support/Blocklight Launcher/instances/`，沿用早期原型的数据目录以保留已有资料。配置和日志也在此目录。`THIRD_PARTY.md` 列出依赖及许可。

## 应用内更新（macOS）

“设置 → 关于”会在启动后检查一次更新，并每六小时再检查。用户也可以点击“检查更新”；发现新版后点击“下载更新”，验证完成后点击“安装并重启”。更新包经过 HTTPS 下载、SHA-256 校验、解压路径检查、应用标识与版本检查，以及 macOS 代码签名验证。旧版保留在应用旁的 `.Glow Craft Launcher.previous.app`，用于手动回退。

更新需要一个固定的 HTTPS `update.json` 发布地址。首次发布时可将地址写进 `update-channel.json` 后重新构建；构建包预设了 `https://github.com/Na1onp/Glow-Craft-Launcher/releases/latest/download/update.json`。在仓库与首个 Release 发布前，此地址会返回 404。也可以在“设置 → 关于”中填写并保存其他更新源地址。后续版本只需上传新的 ZIP，并更新同一地址的 JSON：

```json
{
  "schema": 1,
  "version": "0.6.0",
  "notes": "更新说明",
  "platforms": {
    "macos-arm64": {
      "url": "https://example.com/Glow-Craft-Launcher-macOS.zip",
      "sha256": "64 位十六进制 SHA-256",
      "size": 123456789
    }
  }
}
```

`version` 必须高于当前应用版本，`size` 是 ZIP 的字节数。ZIP 顶层必须是 `Glow Craft Launcher.app`，推荐在 app 所在目录执行 `zip -yr Glow-Craft-Launcher-macOS.zip 'Glow Craft Launcher.app'`，以保留框架符号链接和可执行权限。把应用放在当前用户有写入权限的“应用程序”文件夹中，才能自动替换；系统级受保护位置可能需要先手动移动应用。请勿把不可信的更新地址填入设置。
