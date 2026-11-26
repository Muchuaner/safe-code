# Safe Code for Windows

## 安裝

執行發佈頁中的 `SafeCode-版本-Setup-x64.exe`。支援 Windows 10 2004（19041）及更新版本、Windows 11 x64。安裝到當前用戶的應用目錄，預設建立桌面和開始選單捷徑。安裝包自帶 .NET 和 WebView2 離線執行階段。[下載 Windows 安裝包](https://github.com/Muchuaner/safe-code/releases)

## 連線和部署

1. 填寫 Ubuntu VPS 的 SSH 地址、連接埠和管理員登入資料。預設 SSH 連接埠為 22，也可以在「進階連線」修改。首次連線，或伺服器重新安裝後指紋變化時，請先核對指紋再確認。
2. 部署要求 Ubuntu 22.04 或 24.04 x86_64、可用的 systemd，以及 root 或具備免密 sudo 的登入帳戶。建議至少 4 GB 記憶體和 8 GB 可用磁碟。
3. 分別在「自訂」中儲存工作帳戶與本地化配置，再選擇組件並開始部署。安裝任務在伺服器上繼續執行，客戶端重新連線後可查看進度。

在維護模式中可以管理工作帳戶、開啟 SSH 終端機、連線遠端桌面或啟動 CloudCLI。CloudCLI 首次開啟時，請自行建立網頁帳戶，並按需要用自己的帳號登入 Claude Code。SSH 終端機依賴 Windows OpenSSH Client，精簡系統可在系統的可選功能中啟用。

登入資料按當前 Windows 帳戶加密儲存。若在伺服器端修改過 SSH 登入密碼，回到主介面點擊「更新登入密碼」，驗證同一伺服器身份和新密碼後，可恢復已配置的通道。

## 遠端桌面和目錄掛載

遠端桌面透過已驗證的 SSH 隧道連線。可選擇原始大小、等比縮放、動態解像度或全螢幕。全螢幕時把滑鼠移到頂部可開啟工具列，也可按 `Ctrl+Alt+Enter` 退出全螢幕。

要把 Windows 截圖貼進雲桌面，先點雲端文件或聊天輸入框，在本機用 `Win+Shift+S` 截圖，再點遠端桌面工具列的「貼上本機圖片」。按鈕右側的箭頭可以為當前視窗開啟「Ctrl+V 貼上本機圖片」，重新開啟視窗時該選項預設關閉。文字、檔案和富文本繼續使用遠端桌面的常規貼上。

圖片透過當前桌面的 SSH 連線交給 XFCE 剪貼板。程式優先提供本機已有的圖片格式，Windows 截圖只提供位圖時會生成 PNG。接收應用選擇它支援的格式，單次傳輸的各格式合計最多 32 MiB。

目錄掛載支援從本機選擇資料夾，並在伺服器端選擇工作帳戶下的新目錄或空目錄。掛載使用 SSH 傳輸。伺服器需預留足夠的檔案快取空間，使用期間請保持 Safe Code 執行。退出前在系統匣中選擇「安全退出」，等待同步和卸載完成。

## Claude Desktop 接入


透過 SSH 登入 VPS 後，在維護模式開啟 Claude Desktop 的「網關設定」，點擊「啟用」，再進入「管理網關」建立 API Key。

回到 Safe Code 主介面或登入頁，在「API Key 接入」貼上完整密鑰，點擊「驗證並啟用」。已儲存的 Key 可以更換、重新啟用或停用。首次接入或修改配置後，請完全退出並重新開啟 Claude Desktop。聊天期間請保持 Safe Code 執行。[網關與 Key 使用說明](https://github.com/Muchuaner/safe-code/blob/main/tools/desktop-api/README.md)

如果 Windows 拒絕寫入 Claude Desktop 的當前用戶策略，請先安全退出 Safe Code，再用同一個 Windows 帳戶右鍵 Safe Code，選擇「以管理員身份執行」後重試。若電腦由組織管理，請聯繫管理員確認策略權限。

## 更新和回饋

開啟「更新與回饋」可檢查版本、提交回饋，以及選擇是否啟用基礎使用統計和匿名功能診斷。這兩項預設關閉。[網上更新](https://github.com/Muchuaner/safe-code/blob/main/docs/UPDATES.md) · [匿名功能診斷](https://github.com/Muchuaner/safe-code/blob/main/docs/DIAGNOSTICS.md)
