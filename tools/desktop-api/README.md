# Safe Code API

Safe Code 的 Claude Desktop 網關透過同一台 VPS 上的 CloudCLI 處理對話。CloudCLI 負責調用 Claude Code。Safe Code 只傳送當前任務、會話資訊及已授權的裝置和目錄說明。

在 Safe Code 透過 SSH 登入 VPS 後，開啟「公網網關」，點擊「啟用」。Safe Code 會檢查同一台 VPS 上的 CloudCLI，按需安裝或升級網關，並啟用公網。首次註冊 CloudCLI 時，Safe Code 會自動建立後端 API Key，並顯示生成的網頁帳號和密碼。已有 CloudCLI 帳戶但尚未配置網關時，在「啟用」視窗填入 CloudCLI 設定中生成的 API Key。驗證連線不傳送模型任務。網關使用公網 IP 與當前連接埠簽發 Safe Code API Key。

公網啟用後，從「管理網關」建立 Safe Code API Key，再回到主介面「API Key 接入」貼上。升級會保留原監聽連接埠、TLS 證書、Safe Code API Key、用量及公網啟用狀態。

在「網關總覽 → 前置審查」選擇內置 Claude Sonnet、Opus 或獨立的 DeepSeek Flash，開啟「安全審查」並儲存。安全審查首次安裝預設關閉，升級保留已有選項。網關 revision 10 新增預設關閉的「語言替換」。開啟後，所選模型會將通過安全審查的本輪提示詞翻譯成英文，再交給 CloudCLI。 [前置審查機制](../../docs/PROMPT_REVIEW.md)

自動建立帳戶的網關支援 Desktop 工具審批。只配置 CloudCLI API Key 時，對話走 CloudCLI Agent API，附件、計劃確認和互動式工具審批需要 CloudCLI 的網頁授權。公網 Key 僅供自用。帶有固定工作站配置的網關繼續透過原有迴環監聽和受信任的 HTTPS 入口執行。可查看 [CloudCLI 工作區與裝置控制配置](../../docs/CLOUDCLI_GATEWAY.md)。

用量分析可切換近 1 小時、24 小時和 7 天，查看請求與 Token 吞吐、處理和排隊耗時分佈、請求結果以及錯誤與限流趨勢。併發與排隊顯示實時狀態，模型用量顯示歷史累計值。圖表使用網關已有的請求日誌，明細保留 30 天，列表可查看最近 30 條。請求日誌只儲存模型、密鑰 ID 等統計欄位。

新部署的 Safe Code API 處理槽位預設為 0，表示不限制同時處理的 Messages 請求數。網關的連線保護和密鑰限額獨立生效。管理員可以在「網關總覽 → 處理槽位」儲存新的上限，所有密鑰共享槽位。調低上限時，已經開始的請求繼續處理。其餘請求按到達順序排隊，最多 12 個，同一個 Key 最多等候 2 個，最長等候 45 秒。滿隊、超時或超出每分鐘限制時返回 429。限頻統計滾動 60 秒內獲准執行的請求，0 表示不限頻，服務重新啟動會重設頻率視窗。Claude Desktop 等待工具結果時，不佔據 HTTP 請求佇列。佇列設計參考 [sub2api 的併發服務](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/concurrency_service.go) 中的名額與等待上限。管理只經已連線的 SSH 調用 Unix 套接字，公網連接埠提供模型查詢、Messages 和僅限本人 Key 的餘額查詢。

網關按 [Claude API 官方標準價](https://platform.claude.com/docs/en/about-claude/pricing) 估算輸入、輸出、快取讀取和快取寫入費用。缺少快取時長時按 5 分鐘寫入價估算。歷史數據用當前標準價補算，Haiku 5.5 的歷史彙總按基礎價估算，因為已有記錄缺少每次請求的輸入明細。費用欄顯示已估算的金額。該金額用於網關額度管理，Claude 訂閱和實際帳單由服務商單獨計算。使用 API Key 接入的裝置可以點擊系統狀態列或 Windows 系統匣圖示，查看該 Key 的已用金額和網關餘額。

建立或編輯密鑰時可填寫累計美元額度，留空不限額。餘額等於累計額度減已用金額。請求完成後扣除實際觀測用量的估算費用，餘額用完則後續 Messages 請求返回 HTTP 402。一筆在餘額為正時開始的請求可能使餘額變為負數。提高累計額度即可補充餘額。

手動安裝時，將本目錄的 `install.sh`、`server.py`、`management.py`、`claude_usage.py`、`pricing.py`、`configure_cloudcli.py`、`host_control_mcp.py`、`device_bridge.py`、`prompt_review.py`，以及 `src/SafeCode.Core/Remote/` 中的 `desktop_gateway.py` 和 `codex_bridge.zip` 放在同一目錄。將範例中的帳戶名、監聽地址和連接埠替換為你的配置，再以管理員身份執行以下命令。

```sh
bash install.sh workspace 192.0.2.10 45143
systemctl status safe-code-desktop-api
```

在客戶端啟用公網網關後，也可在 Linux 工作帳戶下籤發一份 Key。用實際可達的 HTTPS 地址替換範例域名，簽發命令會將 Key 寫入指定的新檔案，並輸出可用於撤銷的 Key ID。透過私密渠道將檔案交給裝置的使用者。

在「網關管理 → 密鑰管理」建立或編輯密鑰時，可以為單把 Key 選擇允許的 CloudCLI 模型和失效時間。新密鑰及升級前簽發的密鑰預設允許全部模型、永不過期。開啟模型限制後，網關只返回獲准的模型，並在請求執行前校驗模型。選擇 CloudCLI 的 Default、Best 等自動選項時，由 CloudCLI 決定實際模型。到期後，網關拒絕這把 Key 的新請求和仍在排隊的請求。可以編輯到期的 Key 延長有效期，接入裝置可繼續使用原 Key。

```sh
state="$HOME/.local/state/safe-code-desktop-api"
python3 /opt/safe-code/desktop-api/server.py --state "$state" issue https://gateway.example.com:45143 "$state/laptop.key"
```

在新裝置上安裝官方 Claude Desktop，開啟 SafeCode 登入頁的「API Key 接入」，貼上整行 Key，點擊「驗證並啟用」。首次啟用後完全退出並重開 Claude Desktop。聊天時保持 SafeCode 執行，再次啟動 SafeCode 會恢復本機接入。在同一入口停用可恢復之前選用的 Claude 配置。

自動配置的網關讀取 CloudCLI 的 Claude 模型目錄，包括 Default、Best、Opus Plan、自訂模型和目錄中開放的 1M 選項。網關只有 CloudCLI Agent API Key 時，模型目錄來自 VPS 工作帳戶的 Claude Code。手動配置的網關使用管理員設定的模型及 1M 範圍。升級後，已接入的裝置可在「API Key 接入」點擊「重新整理模型」，再重新啟動 Claude Desktop。

CloudCLI 提供 Ultracode 時，Claude Desktop 的模型選單會顯示對應的「模型名 · Ultracode」選項。選擇它會把 Ultracode 明確傳給 CloudCLI。當前 Claude Desktop 的本地開關可能只傳送 Extra 檔位，無法把遠端工作流模式與普通 Extra 區分開。

Windows 會使用 Claude Desktop 的 3P 策略指向本機轉發，並透過 Windows 應用入口啟動 Claude Desktop。策略中使用本機轉發憑據，公網 Key 仍儲存在 Safe Code 的加密狀態中。停用接入時，Safe Code 撤銷自己寫入且仍未被修改的策略欄位。

新簽發的接入憑證使用加密的 `sc2.` 格式，需要支援該格式的客戶端。新版客戶端仍接受以前簽發的 `sc1.` 憑證。加解密材料隨軟件分發，完整接入憑證仍需要按密鑰保管。

需要撤銷時，在網關管理視窗操作，或在 Linux 工作帳戶下使用簽發時輸出的 ID。撤銷對後續請求生效。已撤銷的密鑰可在管理視窗徹底刪除，相關用量和請求記錄也會一併刪除。若更換 TLS 證書或公網地址，請為裝置簽發新 Key。

```sh
python3 /opt/safe-code/desktop-api/server.py --state "$state" revoke "$key_id"
```
### Key 模式的裝置控制

標準安裝、升級和故障修復會準備官方 FRP 的獨立 `safe-code-desktop-frps.service` 和本機鑑權服務。它們都只監聽 VPS 迴環地址。已有自管 FRP 配置的 VPS 繼續使用原有服務。裝置服務未就緒時仍可以使用普通 API。

建立或編輯密鑰時，可選擇「Safe Code 桌面接入」或「普通 API」。桌面接入需要私有裝置服務健康，原有密鑰可授權配對，無需重新簽發。客戶端透過現有 HTTPS 入口建立 FRP 隧道，仍須經本機明確授權並驗證 VPS 的 SSH 回連，才會儲存 Claude Desktop 配置。改回普通 API 會取消該 Key 的 FRP 裝置配對。

獨立 frps 實例應僅允許受信任的傳輸入口訪問。將代理監聽設為迴環，強制 TLS，並將 `Login`、`NewProxy`、`Ping`、`NewWorkConn` 和 `NewUserConn` 接入 SafeCode 的裝置授權回調。回調檢查裝置憑據有效期、所屬 Key，以及代理類型、別名和連接埠。網關為每台裝置分配單獨的 SSH 身份和項目目錄。選中同名目錄的裝置用不同的 SSH 別名作為項目名稱後綴。裝置證明只隨 SafeCode 發往網關的請求傳輸。
