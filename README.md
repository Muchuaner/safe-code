<p align="center">
  <img src="assets/safe-code-logo.png" alt="Safe Code Logo" width="900">
</p>

<p align="center">
  <b>一個安裝包，把 Ubuntu VPS 配成順手的開發環境</b>
</p>

<p align="center">
  <a href="https://github.com/Muchuaner/safe-code/releases/tag/Beta-0.4.4.2"><img src="https://img.shields.io/badge/下載-Beta--0.4.4.2-c2566f?style=for-the-badge" alt="下載 Beta-0.4.4.2"></a>
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011-e07b2a?style=for-the-badge" alt="Windows 10 / 11">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-6b4a3a?style=for-the-badge" alt="MIT License"></a>
</p>

macOS 版支援 Apple Silicon（arm64），安裝、構建與驗證範圍見 [Mac 使用說明](docs/MACOS.md)。

## ✨ 功能

| | 功能 | 說明 |
|:--:|:---|:---|
| 🚀 | **一鍵部署** | 工作用戶、中文環境、XFCE 桌面、Chrome、拼音輸入法、Claude Code / Git / tmux、SSH 密鑰，可單選或批次安裝 |
| 🔄 | **斷線不中斷** | 安裝任務在 VPS 上獨立執行，重新連線後繼續查看進度 |
| 🖥️ | **遠端連線** | 一鍵開啟遠端桌面和 SSH 終端機 |
| 📂 | **目錄掛載** | 多組映射、大檔案傳輸、斷線守護、新建目錄與精簡系統匣；自動聯動 CloudCLI 項目 |
| 🔐 | **本機遠端控制** | 經權限確認建立專用 SSH 通道，讓伺服器 AI 連接本機命令列；預設關閉 |
| 🧩 | **Codex接入** | 可選第三方適配器，提供獨立供應商配置；預設關閉，真實模型相容性尚未驗收 |
| 🌐 | **CloudCLI** | 安裝 CloudCLI，並透過 SSH 隧道一鍵開啟網頁 |
| 🛡️ | **IP 管理** | 只選擇伺服器檢測到的 IP，獨立應用並驗證出口，失敗自動回退 |
| 🧭 | **時區與語言** | 手動選擇時區和系統語言 |
| ⬆️ | **網上更新** | 標題與底部醒目提示新版本，校驗安裝包後由你確認安裝 |
| 🧰 | **故障診斷** | 預設關閉，主動開啟後僅上報固定步驟與錯誤類型，不上載原始日誌、憑據或路徑 |

## 貢獻者

- [yziyan123](https://github.com/yziyan123) 提供 Windows 遠端桌面動態解像度、全螢幕和固定解像度下的等比縮放方案，見 [#4](https://github.com/Muchuaner/safe-code/pull/4) 和 [#8](https://github.com/Muchuaner/safe-code/pull/8)。
- [SuperGh2233](https://github.com/SuperGh2233) 提供 macOS 遠端桌面的 SSH 隧道複用、穩定入口、RDP 探活和客戶端檢測方案，見 [#5](https://github.com/Muchuaner/safe-code/pull/5)。

---

<p align="center">
  <a href="docs/WINDOWS.md">使用說明</a> · <a href="https://qm.qq.com/q/cyUWAwYKQg">QQ 群</a> · <a href="https://www.rainyun.com/Agentom_">VPS 推薦</a>
</p>
