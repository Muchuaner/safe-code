# Safe Code for macOS

## 安装

支持 Apple 芯片（arm64）和 macOS 15 或更新版本。下载 `SafeCode-版本-macOS-arm64.dmg`，打开镜像后将 Safe Code 拖入「应用程序」。安装包自带 .NET，界面由系统的 WKWebView 和 AppKit 提供。[下载 macOS 安装包](https://github.com/Muchuaner/safe-code/releases)

如果发布说明标明该版本尚未通过 Apple 公证，macOS 可能会拦截首次启动。核对来源和安装包 SHA-256 后，可在系统设置的「隐私与安全性」中按提示允许打开。

## 连接和部署

打开 Safe Code 并填写 Ubuntu VPS 的 SSH 地址、端口和管理员登录信息。首次连接时核对主机指纹。部署要求 Ubuntu 22.04 或 24.04 x86_64、可用的 systemd，以及 root 或具备免密 sudo 的登录账户。

在部署页分别保存工作账户和本地化设置，再选择要安装的组件。重新连接同一台服务器后可以继续查看部署进度。维护模式提供 SSH 终端、远程桌面、目录挂载和 CloudCLI。

SSH 终端调用 macOS 的 Terminal 和 SSH 客户端。登录信息与工作账户草稿在本机加密保存，密钥保存在登录钥匙串。更新 App 会保留本机加密登录记录。若 macOS 要求重新核验钥匙串权限，登录页会显示「解锁已保存的登录信息」，点击后按系统提示授权即可恢复。

## 远程桌面和目录挂载

在维护模式点击「打开桌面」，Safe Code 会通过已验证的 SSH 隧道使用服务器的工作账户登录。安装包内置远程桌面组件，凭据通过本机进程管道传入。可在「分辨率设置」中选择原始大小、等比缩放、动态分辨率及启动时全屏。

目录挂载支持多组本机目录与服务器目录。关闭主窗口后，Safe Code 继续在后台运行。点击屏幕右上方的终端形菜单栏图标，可以查看状态、打开主界面、重连或安全退出。首次关闭主窗口时，macOS 会询问是否允许通知。允许后，点击后台运行通知也能回到主界面。安全退出会等待目录同步和卸载完成。

## Claude Desktop 接入


通过 SSH 登录 VPS 后，在维护模式打开 Claude Desktop 的「网关设置」，点击「启用」，再进入「管理网关」创建 API Key。

回到 Safe Code 主界面或登录页，在「API Key 接入」粘贴完整密钥，点击「验证并启用」。已保存的 Key 可以更换、重新启用或停用。首次接入或修改配置后，请完全退出并重新打开 Claude Desktop。聊天期间请保持 Safe Code 运行。[网关与 Key 使用说明](https://github.com/Muchuaner/safe-code/blob/main/tools/desktop-api/README.md)

## 更新和反馈

可在应用菜单打开「更新与反馈」，手动检查版本、提交反馈，或选择是否开启使用统计和匿名功能诊断。更新仅匹配 Apple 芯片安装包。下载并校验后退出旧版，再打开镜像替换应用。[在线更新](https://github.com/Muchuaner/safe-code/blob/main/docs/UPDATES.md) · [匿名功能诊断](https://github.com/Muchuaner/safe-code/blob/main/docs/DIAGNOSTICS.md)

## 本地构建

使用 Apple Silicon、Apple Command Line Tools、.NET 10 SDK 和 CMake 构建安装包。构建工具核验上游源码及用于静态链接的 Apple Silicon OpenSSL 构建包，随应用分发 FreeRDP、OpenSSL、SDL3、SDL3_ttf、FreeType 以及 Open Sans 的许可证。
