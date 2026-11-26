# Claude Desktop 經 CloudCLI 訪問 Mac

## 網關架構約束

API Key 接入的唯一模型調用鏈路是 **SafeCode 網關 → CloudCLI → Claude Code**。由 CloudCLI 啟動 Claude Code。CloudCLI 使用獨立發佈的上游版本，網關安全改動由 SafeCode 的請求處理、裝置授權和部署配置實現。CloudCLI 不可用時，網關直接返回錯誤。

舊後端的 `DESKTOP_LAUNCHER`、`Channel`、`Conversation` 及 `Manager` 中的直啟分支都標記為**待刪除**。禁止恢復、擴展或將它們用作相容回退。網關內直接啟動 CLI 的模型目錄和訂閱用量探測也待遷移並刪除。

## 請求鏈路

CloudCLI 模式把 Claude Desktop 當作聊天介面。SafeCode 收到 Messages 請求後，從最後一條用戶消息提取任務，再交給同一台 VPS 上的 CloudCLI。CloudCLI 自己啟動 Claude Code，構建原生系統提示並載入項目工具。對於已綁定的本機目錄，SafeCode 在會話首輪附加裝置、項目根目錄和現有執行通道的說明。續聊使用同一 CloudCLI 會話和原有上下文。已有會話升級後會在下一輪補發一次。若編輯歷史刪掉了攜帶說明的消息，網關會在替換消息中補回當前說明。

Claude Desktop 的系統提示、客戶端工具定義、模型輸出預算、全量歷史和授權資料不隨這條請求轉發。SafeCode 在本地處理 Desktop 的網關連通性探測、起名和分支名建議。探測只驗證 Desktop 到 SafeCode 的連通性。CloudCLI 用首條消息的開頭生成會話標題，因此 SafeCode 將實際任務放在裝置說明之前。短任務的標題仍可能帶上部分裝置說明。

## CloudCLI 功能清單與 Desktop 適配

以下是 CloudCLI 自託管 1.37.8 的功能和 SafeCode 的適配情況。部分功能需要先開啟網關與 CloudCLI 的已認證連線。對話流使用 Messages SSE，執行詳情可在 CloudCLI 原會話查看。

### 對話與執行

| CloudCLI 功能 | Claude Desktop 適配情況 |
| --- | --- |
| 文本聊天、流式輸出和續聊 | **已接通。** 保留助手輸出順序和 CloudCLI 會話綁定，區分完成、上游失敗和取消 |
| 項目歸類、工作目錄和裝置隔離 | **已接通。** 按 Desktop 工作目錄歸到對應的 CloudCLI 項目，同名目錄按裝置隔離 |
| 會話標題、分支名建議和連通性探測 | **已接通。** 網關本地處理 Desktop 起名與探測，普通對話的 CloudCLI 標題取首條任務開頭。建議回覆當前返回空結果 |
| 模型目錄、同會話切換、推理強度和 1M 上下文 | **已接通。** 自動配置的網關讀取 CloudCLI 模型目錄及其檔位和 1M 選項，手動配置使用管理員指定的模型範圍。已認證的會話切換透過 CloudCLI 聊天接口更新同一會話的模型 |
| 思考狀態、上下文壓縮和截斷 | CloudCLI 上報的主助手思考以原生 Messages 思考塊顯示，壓縮和截斷以文字提示 |
| 工具執行、子代理、背景任務和工作流 | Desktop 顯示簡短的執行提示和彙總。完整參數、輸出、逐項摺疊及停止單個背景任務在 CloudCLI 原會話中處理 |
| 選擇題 `AskUserQuestion` | **原生接通。** 單選、多選、自訂答案和跳過回到 CloudCLI 當前待答的執行 |
| 計劃模式和 `ExitPlanMode` | **原生接通。** Desktop 進入計劃模式後，CloudCLI 也以計劃模式執行。網關收到完整計劃正文後，先呈現正文，再用 Desktop 的原生確認接收批准或拒絕 |
| 工具審批、記住許可和 MCP 表單 | **部分接通。** 開啟一次性授權後，CloudCLI 發來的工具審批會在 Desktop 展示真實參數，並提供允許一次或拒絕。持久許可和 MCP 表單由 CloudCLI 管理 |
| 貼上截圖、圖片和上載文件 | **已接通。** 內嵌 PNG、JPEG、GIF、WebP、PDF 和支援的文本附件透過 CloudCLI 上載接口隨消息傳送。單檔案上限 5 MiB，整體受 Messages 請求上限約束 |
| 生成圖片和檔案的預覽、下載 | **部分接通。** 文本輸出可見，二進制結果提示在 CloudCLI 查看 |
| 停止生成 | **已接通。** Desktop 關閉正在執行的流後，網關校驗 CloudCLI 的會話與本輪啟動時間，再傳送取消。收到確認後保留續聊關聯。等待選擇框或計劃確認時，用介面裏的拒絕或跳過操作 |
| 編輯已傳送消息後重發 | **已接通。** 同一 Desktop 會話可透過已驗證的 CloudCLI 消息錨點回到編輯位置。啟用後記錄的新消息可映射錨點。遠端歷史發生變化時會提示衝突 |
| 分支和執行中插話 | **尚未接入。** 分支需要可靠的父會話資訊。真正的插話需要 Desktop 傳出區別於排隊傳送的動作 |
| Token 用量、上下文視窗和預先分詞 | **部分接通。** 已展示執行後用量及上下文視窗。精確分詞仍需提供方專用接口 |

### CloudCLI 工作區和管理功能

| CloudCLI 功能 | Claude Desktop 適配情況 |
| --- | --- |
| 項目列表、收藏、歸檔、恢復、重命名 | 網關負責項目歸類。這些管理操作在 CloudCLI |
| 歷史發現、檢索、歸檔、恢復、匯出和會話複製 | Desktop 發起的對話可繼續原 CloudCLI 會話。歷史庫、搜尋和匯出各自由客戶端管理 |
| 會話內側邊提問、草稿、佇列和定時消息 | CloudCLI 管理。Desktop 目前接入主會話的同步響應 |
| 終端機、檔案樹、編輯器和檔案管理 | 模型可透過已授權的 SafeCode 本機控制操作選定目錄。CloudCLI 的互動終端機和檔案面板仍在 CloudCLI |
| Git 狀態、差異、提交、分支、Worktree 和 GitHub 操作 | CloudCLI 管理介面提供這些功能。對話裏能完成的命令取決於執行帳戶和工具權限 |
| Claude Code 斜槓命令、Skills、MCP 和項目記憶檔案 | CloudCLI 執行階段按 VPS 的項目和用戶配置使用。Desktop 自己的選單、插件和帳號記憶各有配置 |
| 瀏覽器會話、語音轉寫和朗讀 | CloudCLI 自帶介面提供，取決於已啟用的執行階段與服務 |
| 通知、Web Push、Webhook 和用戶偏好 | CloudCLI 管理。Desktop 收到的是當前對話的流式事件 |
| Cursor、Codex、OpenCode、多端介面、CloudCLI 插件和可選 TaskMaster | CloudCLI 功能。SafeCode 的 Claude Desktop 接入使用 Claude Code 提供方 |
| 帳戶登入、憑據、實例配置和 CLI 更新 | 由 CloudCLI 管理。Claude 官方帳號的跨裝置歷史與帳號偏好仍由 Claude 帳戶服務管理 |

啟用原生選擇題後，支援選擇題的 Desktop 會話透過 CloudCLI 已認證的聊天接口執行，純文本消息也可以彈出選擇框。附件與計劃模式同樣使用這個接口。其他普通純文本請求繼續使用 CloudCLI 的 Agent API。SafeCode 利用 CloudCLI 的已認證控制通道提交選擇、計劃確認及停止指令。審批需要和同一輪裏的待處理請求匹配，附件儲存在 CloudCLI 的上載目錄。

在 SafeCode 點擊「開啟 Claude Desktop」時，客戶端會核對當前接入配置，並透過本機轉發器查詢網關的模型目錄。若 Claude 早於本次接入啟動，請完全退出後再開啟。Windows 支援已註冊的 Claude 應用和帶 Anthropic 簽名的獨立桌面程式，macOS 按應用標識啟動。未找到應用時會開啟[Claude Desktop 官方下載頁](https://claude.com/download)，安裝後可以回到 SafeCode 重試。

僅配置 CloudCLI Agent API Key 的網關會在 Desktop 斷線後繼續接收這一輪的結果。重新連線時，如果任務仍在執行，請等待或在 CloudCLI 中停止它。完成後，在原對話重試可以取回結果，網關不會重複提交任務。截圖、附件、計劃模式和從 Desktop 停止雲端任務需要管理員先連線已有的 CloudCLI 網頁帳戶，配置方法見下文。

## 準備

需要 VPS 上的 CloudCLI 1.37.7 或 1.37.8。Mac 透過 SafeCode 已啟用的「本機遠端控制」連線到同一台 VPS。Mac 需有可用的 `/usr/bin/python3`，且 macOS 已允許該 SSH 登入帳戶訪問所選目錄。CloudCLI 用的是工作用戶配置的 Claude 認證。面向用戶的服務使用 Claude Console API Key 或支援的雲提供商認證。[Anthropic 認證說明](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)

更新 VPS 網關時，把本倉庫 `tools/desktop-api/` 下的安裝腳本及其 Python 檔案，與 `src/SafeCode.Core/Remote/` 下的 `desktop_gateway.py`、`codex_bridge.zip` 放到同一目錄。已有固定工作站配置的網關，使用 [網關安裝腳本](../tools/desktop-api/install.sh) 保持監聽地址為 `127.0.0.1`，繼續使用原 HTTPS 入口或 SSH 通道。新部署的公網直連網關可透過 Safe Code 的「公網網關」安裝和授權。自動建立帳戶的網關使用 CloudCLI 的標準工具審批。

在 Mac 啟用「本機遠端控制」，然後用 CloudCLI 服務的 Linux 工作帳戶執行配置工具。以下目錄是公開範例，替換為目標 Mac 上的目錄：

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py \
  --workspace /Users/example/Desktop \
  --device-name "MacBook Air" \
  --model claude-haiku-5-5 --model claude-opus-5-5
```

提示出現後貼上 CloudCLI 設定頁生成的 Key。配置程式自動複用唯一可用的 SafeCode 控制身份。有多個連線時，可用 `--client-id` 指定已授權的連線。預設建立獨立的 VPS 項目目錄，`--project` 可以指定已有項目。若要把 CloudCLI 項目代理限制在專用工作目錄和帳戶，請使用專用 CloudCLI 工作帳戶和獨立項目。CloudCLI 自帶的 Agent API 在 VPS 項目內執行工具時啟用 `bypassPermissions`。[Agent 路由原始碼](https://github.com/siteboon/claudecodeui/blob/7e7452219d788847b607d369728085995db719f1/server/modules/agent/agent.routes.ts#L1044-L1051)

### 開啟 Desktop 的互動和附件

在 VPS 網關使用的 Python 環境安裝 `websocket-client`。Debian 和 Ubuntu 使用系統包 `python3-websocket`。已部署的網關可以用同一個工作帳戶執行以下命令開啟原生選擇框、計劃確認、附件和停止生成。

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py --desktop-questions
```

按提示輸入已有的 **CloudCLI 網頁帳戶**。SafeCode 透過本機 CloudCLI 登入接口換取短期會話令牌，登入資料儲存在 VPS 工作帳戶權限為 `0600` 的網關配置中。Desktop 用戶照常使用原有 SafeCode API Key。重新載入網關後，用戶可以在 Desktop 直接完成選擇和計劃審批。待答請求最多保留五分鐘。計劃正文缺失或超過完整呈現上限時，可到 CloudCLI 查看並處理對應請求。用 `--no-desktop-questions` 清除此配置。

使用迴環監聽的自管網關也可以由伺服器管理員提供短期 CloudCLI 會話令牌。將網關配置的 `desktopQuestions` 設為 `{"tokenFile":"/run/safe-code-cloudcli-session/session.jwt"}`。令牌檔案由網關工作帳戶持有，權限為 `0600`，有效期不超過五分鐘，由本機受信服務在到期前原子替換。SafeCode 只將它傳送到本機 CloudCLI。公網直連模式不接受這項配置。

同會話切換模型和編輯重發也使用此認證。可編輯的消息需要在已啟用認證的網關上完成並記錄錨點。編輯僅調整對話歷史，檔案或終端機產生的變更按其各自的操作處理。

### API Key 的工具權限

透過 Safe Code API 網關接入時，新簽發和已存在的 Key 在普通對話中都使用 CloudCLI 的原生無限制工具模式。為裝置配對前，請確認它可以使用該帳戶的工作目錄和工具。計劃確認和需要用戶選擇的問題會繼續顯示互動卡片。管理員可獨立配置網關前置審查。

卡片顯示期間切換模型時，提交的選擇仍會送給發出該卡片的執行任務。下一條普通消息會使用新選的模型。已結束的提問會提示重新提問。

### 啟用 1M 上下文

確定 CloudCLI 登入的 Claude 提供方已經開放該型號的 1M 上下文後，用對應的網關允許列表型號開啟。這裏使用範例型號。

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py --long-context-model claude-opus-5-5
```

有多個型號時可重複傳入 `--long-context-model`。重載網關並重新整理 Desktop 的網關接入配置後，已啟用型號才會顯示 1M 能力。SafeCode 用原型號的 `[1m]` 變體發給 CloudCLI，並在對話中顯示 CloudCLI 上報的視窗大小。若回覆上報了不同型號，Desktop 會顯示型號差異。用 `--clear-long-context-models` 清除 1M 配置。

可透過 `--web-origin https://cloud.example.com` 指定 CloudCLI 網頁的 HTTPS 地址，供計劃確認、工具審批以及較舊客戶端的提示開啟原會話。省略該選項時，提示顯示會話 ID。

配置完成後，重新啟動 SafeCode API 服務，再使用 API Key 接入 Claude Desktop。準備階段會透過已授權通道檢查一次目標目錄的讀取權限，不發模型消息。CloudCLI 模式啟用後，CloudCLI 或 Mac 控制連線出錯會直接返回失敗。

## Mac 檔案權限

CloudCLI 項目中的 `safecode_host` MCP 工具透過原有通道操作選定目錄。可以列出目錄、讀取及新建 UTF-8 文本檔案，以及移動檔案或資料夾。移動不覆蓋已有目標，路徑遍歷和符號連結跨目錄會被拒絕。CloudCLI 原生檔案工具執行在 VPS，因此處理 Mac 檔案時應選用這一組工具。已有「本機遠端控制」可用來管理更廣的裝置操作。

用 `--workspace` 指定主工作區，用 `--allow-workspace` 添加其他可訪問的目錄。SafeCode 按會話的實際選擇將檔案工具限定在對應資料夾。授權範圍之外的目錄可以歸檔對話。檢查工作區或在背景保活時無需傳送模型消息。

自用部署可在配置時加上 `--allow-ssh-shell`。網關會從現有的 SafeCode 本機遠端控制配置讀取 SSH 命令，並在已授權項目的任務說明中提供給 CloudCLI。該模式取消 SafeCode 為這些項目註冊的檔案工具。透過 SSH 執行的命令使用 Mac 登入帳戶的權限。

要移除此綁定，以同一個 VPS 工作帳戶執行以下命令，再重新啟動網關或重新開啟 SSH 接入：

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py --disable
```
