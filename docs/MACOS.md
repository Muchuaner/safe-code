# Safe Code for macOS

## 安装

支持 Apple 芯片（arm64）和 macOS 15 或更新版本。下载 `SafeCode-版本-macOS-arm64.dmg`，打开镜像后将 Safe Code 拖入「应用程序」。安装包自带 .NET，界面由系统的 WKWebView 和 AppKit 提供。[下载 macOS 安装包](https://github.com/Muchuaner/safe-code/releases)

如果发布说明标明该版本尚未通过 Apple 公证，macOS 可能会拦截首次启动。核对来源和安装包 SHA-256 后，可在系统设置的「隐私与安全性」中按提示允许打开。

## 连接和部署

打开 Safe Code 并填写 Ubuntu VPS 的 SSH 地址、端口和管理员登录信息。首次连接时核对主机指纹。部署要求 Ubuntu 22.04 或 24.04 x86_64、可用的 systemd，以及 root 或具备免密 sudo 的登录账户。

在部署页分别保存工作账户和本地化设置，再选择要安装的组件。重新连接同一台服务器后可以继续查看部署进度。维护模式提供 SSH 终端、远程桌面、目录挂载和 CloudCLI。

SSH 终端调用 macOS 的 Terminal 和 SSH 客户端。登录信息与工作账户草稿在本机加密保存，密钥保存在登录钥匙串。需要读取已保存的登录信息时，按 macOS 提示授权即可。

## 远程桌面和目录挂载

远程桌面复用已验证的 SSH 隧道。可在「分辨率设置」中选择原始大小、等比缩放、动态分辨率及启动时全屏。安装 SDL FreeRDP 3 时可由 Safe Code 提交工作账户凭据。使用 Windows App 或其他默认 RDP 客户端时，由相应客户端处理凭据和显示设置。

目录挂载支持多组本机目录与服务器目录。挂载使用期间请保持应用运行。菜单栏提供显示主界面、重连和安全退出，退出前等待目录同步和卸载完成。

## Claude Desktop 接入

要使用已签发的 Key，打开「Safe Code API 接入」，粘贴 Key 并点「验证并启用」。已保存的 Key 可以更换、重新启用或停用。操作完成后，请完全退出并重新打开 Claude Desktop。[网关与 Key 使用说明](https://github.com/Muchuaner/safe-code/blob/main/tools/desktop-api/README.md)

若使用 SSH 接入，连接 VPS 后从维护模式打开「本地 Claude Desktop 接入」的设置，按提示启用。使用任一接入方式时请保持 Safe Code 运行。

## 更新和反馈

可在应用菜单打开「更新与反馈」，手动检查版本、提交反馈，或选择是否开启使用统计和匿名功能诊断。更新仅匹配 Apple 芯片安装包。下载并校验后退出旧版，再打开镜像替换应用。[在线更新](https://github.com/Muchuaner/safe-code/blob/main/docs/UPDATES.md) · [匿名功能诊断](https://github.com/Muchuaner/safe-code/blob/main/docs/DIAGNOSTICS.md)
