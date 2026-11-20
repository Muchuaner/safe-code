# Safe Code

<p align="center">
  <img src="assets/safe-code-logo.png" alt="Safe Code" width="560">
</p>

Safe Code 是一个 Windows 和 macOS 桌面应用，用于部署和维护 Ubuntu VPS。你可以安装工作账户、远程桌面、Claude Code 和 CloudCLI，在同一个界面连接 SSH、挂载本地目录和管理服务器。

## 下载

| 平台 | 系统要求 | 安装包 |
| --- | --- | --- |
| Windows | Windows 10 2004 或更新版本，x64 | `SafeCode-版本-Setup-x64.exe` |
| macOS | macOS 15 或更新版本，Apple 芯片 | `SafeCode-版本-macOS-arm64.dmg` |

安装包、版本说明和 `SHA256SUMS.txt` 随 GitHub Release 提供。[查看所有版本](https://github.com/Muchuaner/safe-code/releases)

## 上手

1. 根据平台安装 Safe Code。打开应用后填写 Ubuntu VPS 的 SSH 地址和管理员登录信息，首次连接时核对主机指纹。
2. 在部署页保存工作账户和本地化设置，再选择要安装的组件。任务在服务器上执行，重新连接后可以继续查看状态。
3. 在维护模式打开 SSH 终端、远程桌面或 CloudCLI，也可以配置目录挂载与 Claude Desktop 接入。Windows 可通过可选的第三方适配器配置 Codex 接入。

连接和安装细节见 [Windows 使用说明](docs/WINDOWS.md) 或 [macOS 使用说明](docs/MACOS.md)。需要管理密钥、网关和用量时，见 [Safe Code API](tools/desktop-api/README.md)。[全部使用文档](docs/README.md)

## 反馈与贡献

可以在应用内「更新与反馈」提交问题，也可以在 GitHub 提交 Issue。请附上软件版本、操作系统和复现步骤，并隐去地址、账号、密钥等信息。欢迎通过 Pull Request 改进项目。[GitHub Issues](https://github.com/Muchuaner/safe-code/issues) · [Pull Requests](https://github.com/Muchuaner/safe-code/pulls) · [QQ 群](https://qm.qq.com/q/cyUWAwYKQg)

感谢 [yziyan123](https://github.com/yziyan123) 贡献 Windows 远程桌面显示模式和缩放方案，[SuperGh2233](https://github.com/SuperGh2233) 贡献 macOS 远程桌面连接改进，[anson111231](https://github.com/anson111231) 报告 GitHub 限流时的更新故障并提供补丁及测试。

项目以 MIT 许可证发布。[许可证](LICENSE) · [第三方许可](THIRD_PARTY_NOTICES.md)
