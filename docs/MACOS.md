# Safe Code for macOS

## 安裝

支援 Apple 芯片（arm64）和 macOS 15 或更新版本。下載 `SafeCode-版本-macOS-arm64.dmg`，開啟鏡像後將 Safe Code 拖入「應用程式」。安裝包自帶 .NET，介面由系統的 WKWebView 和 AppKit 提供。[下載 macOS 安裝包](https://github.com/Muchuaner/safe-code/releases)

如果發佈說明標明該版本尚未通過 Apple 公證，macOS 可能會攔截首次啟動。核對來源和安裝包 SHA-256 後，可在系統設定的「私隱與安全性」中按提示允許開啟。

## 連線和部署

開啟 Safe Code 並填寫 Ubuntu VPS 的 SSH 地址、連接埠和管理員登入資料。首次連線時核對主機指紋。部署要求 Ubuntu 22.04 或 24.04 x86_64、可用的 systemd，以及 root 或具備免密 sudo 的登入帳戶。

在部署頁分別儲存工作帳戶和本地化設定，再選擇要安裝的組件。重新連接同一台伺服器後可以繼續查看部署進度。維護模式提供 SSH 終端機、遠端桌面、目錄掛載和 CloudCLI。

SSH 終端機調用 macOS 的 Terminal 和 SSH 客戶端。登入資料與工作帳戶草稿在本機加密儲存，密鑰儲存在登入鑰匙串。更新 App 會保留本機加密登入記錄。若 macOS 要求重新核驗鑰匙串權限，登入頁會顯示「解鎖已儲存的登入資料」，點擊後按系統提示授權即可恢復。

## 遠端桌面和目錄掛載

遠端桌面複用已驗證的 SSH 隧道。可在「解像度設定」中選擇原始大小、等比縮放、動態解像度及啟動時全螢幕。安裝 SDL FreeRDP 3 時可由 Safe Code 提交工作帳戶憑據。使用 Windows App 或其他預設 RDP 客戶端時，由相應客戶端處理憑據和顯示設定。

目錄掛載支援多組本機目錄與伺服器目錄。關閉主視窗後，Safe Code 繼續在背景執行。點擊螢幕右上方的終端機形選單列圖示，可以查看狀態、開啟主介面、重連或安全退出。首次關閉主視窗時，macOS 會詢問是否允許通知。允許後，點擊背景執行通知也能回到主介面。安全退出會等待目錄同步和卸載完成。

## Claude Desktop 接入


透過 SSH 登入 VPS 後，在維護模式開啟 Claude Desktop 的「網關設定」，點擊「啟用」，再進入「管理網關」建立 API Key。

回到 Safe Code 主介面或登入頁，在「API Key 接入」貼上完整密鑰，點擊「驗證並啟用」。已儲存的 Key 可以更換、重新啟用或停用。首次接入或修改配置後，請完全退出並重新開啟 Claude Desktop。聊天期間請保持 Safe Code 執行。[網關與 Key 使用說明](https://github.com/Muchuaner/safe-code/blob/main/tools/desktop-api/README.md)

## 更新和回饋

可在應用選單開啟「更新與回饋」，手動檢查版本、提交回饋，或選擇是否開啟使用統計和匿名功能診斷。更新僅匹配 Apple 芯片安裝包。下載並校驗後退出舊版，再開啟鏡像替換應用。[網上更新](https://github.com/Muchuaner/safe-code/blob/main/docs/UPDATES.md) · [匿名功能診斷](https://github.com/Muchuaner/safe-code/blob/main/docs/DIAGNOSTICS.md)
