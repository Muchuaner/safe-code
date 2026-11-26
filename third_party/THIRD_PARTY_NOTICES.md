# 第三方組件

Safe Code 自有原始碼採用 MIT 許可證。下列依賴保留其原有許可證，打包工具將依賴包內的 LICENSE / NOTICE 原文複製到便攜包的 `licenses/`。

| 組件 | 用途 | 來源與許可 |
| --- | --- | --- |
| SSH.NET 2026.0.0 | SSH、SFTP、連接埠轉發 | https://github.com/sshnet/SSH.NET ，MIT，Copyright © Renci 2010–2026 |
| BouncyCastle.Cryptography 2.7.0 | SSH 加密實現 | NuGet 包內原始許可證 |
| Microsoft.Extensions.Logging.Abstractions / DependencyInjection.Abstractions | 執行庫依賴 | Microsoft，包內原始許可證 |
| Microsoft.Web.WebView2 1.0.4314.35 | Windows 應用介面宿主 | Microsoft，包內 LICENSE.txt（BSD 條款）與 NOTICE.txt |
| Microsoft.NETCore.App / WindowsDesktop.App 10.0 | 自包含執行階段 | Microsoft / .NET contributors，執行階段包內許可證及第三方說明 |

WebView2 Runtime 是 Microsoft 執行環境，不包含在原始碼倉庫或便攜包中。Windows 單 EXE 離線安裝包附帶微軟官方簽名的 Evergreen Standalone Installer，按微軟 WebView2 再分發方式安裝；其許可仍由 Microsoft 提供。遠端桌面使用 Windows 自帶的 Microsoft RDP Client Control，不重新分發 Windows 系統組件。

Ubuntu、XFCE、xrdp、Fcitx5、Google Chrome、Node.js、davfs2、nftables 和 CloudCLI 在用戶明確部署時從各自官方源安裝，不作為本項目自有原始碼或便攜包組件重新打包；其許可證和帳戶條款由原廠提供。特別是 Google Chrome、Claude Code 的使用許可不由本項目的 MIT 許可證替代。

Net.Coffee 為可選 IP 情報來源，評分不是 Anthropic 官方結論。VPS 推薦入口保留 `sponsored` 連結屬性，QQ 與 GitHub 入口不帶該屬性。

部署 Claude Code 時由目標 VPS 從 Anthropic 官方渠道下載對應架構的程式，並按官方清單校驗 SHA-256。Claude Code 受 Anthropic 條款約束，用戶透過原廠流程使用自己的帳戶或 API Key 授權。官方說明：https://code.claude.com/docs/en/legal-and-compliance

本機遠端控制完整測試包附帶 Microsoft PowerShell/Win32-OpenSSH 的未修改 OpenSSH-Win64.zip，版本 10.4.0.0p1-Preview，官方來源：https://github.com/PowerShell/Win32-OpenSSH/releases/tag/10.4.0.0p1-Preview 。OpenSSH 及所含組件按包內 LICENSE.txt / NOTICE.txt 分別授權，構建時同時提取到 licenses。啟用前校驗固定 SHA256，管理員安裝時核驗 Microsoft 數字簽名；未執行其會覆蓋系統服務的 install-sshd.ps1。

Codex 接入 Claude 測試組件基於用戶提供的《Claude-Codex-VPS遷移包.zip》，保留其 MIT LICENSE.txt、Node.js THIRD_PARTY_LICENSES.txt 和 UPSTREAM.txt。包內聲明的上游為 https://github.com/EthanSK/claude-in-codex ，提交 9ad1632ffeeed6b35d89b3476f337d22e15c8ddd（MIT）；Windows 適配器由提供者構建且未數字簽名，並非本項目或 OpenAI / Anthropic 官方構建。原始 Windows EXE 固定 SHA256 c8b2353f14afeef2a04c759d0da64d4aca301127a9e0fcdb82a8032026acd070；伺服器五個 mjs 檔案保留原內容，見 third_party/claude-codex-vps/SOURCE.json。介面另留作者致謝位置，待用戶補充，不替代許可證原作者資訊。

macOS 開發包使用系統 AppKit / WKWebView、Keychain、OpenSSH 與 Terminal，不重新分發 Apple 系統組件；自包含 .NET macOS 執行階段的 LICENSE.TXT / THIRD-PARTY-NOTICES.TXT 隨包保留。Mac 包不含 Windows WebView2、Win32-OpenSSH 或用戶附件的 Windows 模型適配器。Key 接入的本機通道附帶 fatedier/frp 0.65.0 官方 darwin/arm64 `frpc`，上游地址：https://github.com/fatedier/frp/releases/tag/v0.65.0 ，Apache License 2.0；構建時核驗官方歸檔 SHA256，並保留包內 LICENSE。

Claude 與 Codex 板塊使用各自官方站點提供的原始客戶端圖示，分別來自 https://claude.ai/apple-touch-icon.png 與 https://chatgpt.com/codex 頁面引用的 OpenAI 靜態資源，僅用於標識對應客戶端。圖示及商標歸 Anthropic / OpenAI 所有，不屬於 Safe Code 的 MIT 授權範圍，不表示官方合作或背書。來源及 SHA256 記錄於 assets/client-logos.json。
