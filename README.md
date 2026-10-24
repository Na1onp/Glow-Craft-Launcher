# Glow-Craft-Launcher
Glow Craft Launcher 是面向 macOS 与 Windows 的 Minecraft Java 启动器，包含游戏与模组管理、应用内更新，以及好友间的 Minecraft 局域网穿透。

## 联机

房主在 Minecraft 中选择“对局域网开放”，再从 GCL 创建房间。首次使用需要自己的 SakuraFrp 账号访问密钥；GCL 将其存入操作系统钥匙串，并在安装包内运行 frpc。朋友通过 GCL 输入房主地址后，启动器会将地址传给 Minecraft。请只把地址分享给可信好友；此功能当前没有 GCL 房间密码。

## 账户与皮肤

Microsoft 正版登录需要配置启动器的 Microsoft Client ID。离线档案导入的 PNG 用于 GCL 人物预览；原版离线 Minecraft 不会因此加载自定义皮肤，游戏内显示需要另行配置皮肤加载器/皮肤源。

## 发布

macOS 与 Windows 安装包按同一版本号放在 [GitHub Releases](https://github.com/Na1onp/Glow-Craft-Launcher/releases) 的同一个 Release 中。
