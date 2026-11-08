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

Ubuntu、XFCE、xrdp、Fcitx5、Google Chrome、Claude Code、Node.js、davfs2、nftables 和 CloudCLI 在用户明确部署时从各自官方源安装，不作为本项目自有源码或便携包组件重新打包；其许可证和账户条款由原厂提供。特别是 Google Chrome、Claude Code 的使用许可不由本项目的 MIT 许可证替代。

Net.Coffee 为可选 IP 情报来源，评分不是 Anthropic 官方结论。VPS 推荐入口保留 `sponsored` 链接属性，QQ 与 GitHub 入口不带该属性。
