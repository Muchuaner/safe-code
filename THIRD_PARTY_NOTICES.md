# 第三方组件

Safe Code 自有源码采用 MIT 许可证。下列依赖保留其原有许可证，打包工具将依赖包内的 LICENSE / NOTICE 原文复制到便携包的 `licenses/`。

| 组件 | 用途 | 来源与许可 |
| --- | --- | --- |
| SSH.NET 2026.0.0 | SSH、SFTP、端口转发 | https://github.com/sshnet/SSH.NET ，MIT，Copyright © Renci 2010–2026 |
| BouncyCastle.Cryptography 2.7.0 | SSH 加密实现 | NuGet 包内原始许可证 |
| Microsoft.Extensions.Logging.Abstractions / DependencyInjection.Abstractions | 运行库依赖 | Microsoft，包内原始许可证 |
| Microsoft.Web.WebView2 1.0.4314.35 | Windows 应用界面宿主 | Microsoft，包内 LICENSE.txt（BSD 条款）与 NOTICE.txt |
| Microsoft.NETCore.App / WindowsDesktop.App 10.0 | 自包含运行时 | Microsoft / .NET contributors，运行时包内许可证及第三方说明 |

WebView2 Runtime 是 Microsoft 运行环境，不包含在源码仓库或便携包中。Windows 单 EXE 离线安装包附带微软官方签名的 Evergreen Standalone Installer，按微软 WebView2 再分发方式安装；其许可仍由 Microsoft 提供。远程桌面使用 Windows 自带的 Microsoft RDP Client Control，不重新分发 Windows 系统组件。

Ubuntu、XFCE、xrdp、Fcitx5、Google Chrome、Node.js、davfs2、nftables 和 CloudCLI 在用户明确部署时从各自官方源安装，不作为本项目自有源码或便携包组件重新打包；其许可证和账户条款由原厂提供。特别是 Google Chrome、Claude Code 的使用许可不由本项目的 MIT 许可证替代。

Net.Coffee 为可选 IP 情报来源，评分不是 Anthropic 官方结论。VPS 推荐入口保留 `sponsored` 链接属性，QQ 与 GitHub 入口不带该属性。

部署 Claude Code 时由目标 VPS 从 Anthropic 官方渠道下载对应架构的程序，并按官方清单校验 SHA-256。Claude Code 受 Anthropic 条款约束，用户通过原厂流程使用自己的账户或 API Key 授权。官方说明：https://code.claude.com/docs/en/legal-and-compliance

本机远程控制完整测试包附带 Microsoft PowerShell/Win32-OpenSSH 的未修改 OpenSSH-Win64.zip，版本 10.4.0.0p1-Preview，官方来源：https://github.com/PowerShell/Win32-OpenSSH/releases/tag/10.4.0.0p1-Preview 。OpenSSH 及所含组件按包内 LICENSE.txt / NOTICE.txt 分别授权，构建时同时提取到 licenses。启用前校验固定 SHA256，管理员安装时核验 Microsoft 数字签名；未运行其会覆盖系统服务的 install-sshd.ps1。

Codex 接入 Claude 测试组件基于用户提供的《Claude-Codex-VPS迁移包.zip》，保留其 MIT LICENSE.txt、Node.js THIRD_PARTY_LICENSES.txt 和 UPSTREAM.txt。包内声明的上游为 https://github.com/EthanSK/claude-in-codex ，提交 9ad1632ffeeed6b35d89b3476f337d22e15c8ddd（MIT）；Windows 适配器由提供者构建且未数字签名，并非本项目或 OpenAI / Anthropic 官方构建。原始 Windows EXE 固定 SHA256 c8b2353f14afeef2a04c759d0da64d4aca301127a9e0fcdb82a8032026acd070；服务器五个 mjs 文件保留原内容，见 third_party/claude-codex-vps/SOURCE.json。界面另留作者致谢位置，待用户补充，不替代许可证原作者信息。

macOS 开发包使用系统 AppKit / WKWebView、Keychain、OpenSSH 与 Terminal，不重新分发 Apple 系统组件；自包含 .NET macOS 运行时的 LICENSE.TXT / THIRD-PARTY-NOTICES.TXT 随包保留。Mac 包不含 Windows WebView2、Win32-OpenSSH 或用户附件的 Windows 模型适配器。

Claude 与 Codex 板块使用各自官方站点提供的原始客户端图标，分别来自 https://claude.ai/apple-touch-icon.png 与 https://chatgpt.com/codex 页面引用的 OpenAI 静态资源，仅用于标识对应客户端。图标及商标归 Anthropic / OpenAI 所有，不属于 Safe Code 的 MIT 授权范围，不表示官方合作或背书。来源及 SHA256 记录于 assets/client-logos.json。
