# Safe Code 回饋服務

這一目錄提供匿名使用統計、診斷和手動回饋的接收與查詢。客戶端的採集範圍和保留期見 [匿名功能診斷](../../docs/DIAGNOSTICS.md)。

服務使用 Python 標準庫和 SQLite。`server.py` 預設監聽本機迴環地址。執行階段透過 `SAFECODE_PORT` 與 `SAFECODE_DB` 指定連接埠和資料庫檔案。部署時可參考同目錄的 systemd 單元，並用自己的 HTTPS 反向代理對外提供 `/health`、`/v1/usage`、`/v1/feedback` 和 `/v1/diagnostics`。為接收進程設定單獨帳戶，並確保資料庫僅服務帳戶和管理員可讀。

統計和診斷請求採用固定欄位校驗。查詢和彙總在部署主機本地執行，接口無需公開資料庫。

```sh
python3 services/feedback/report.py /path/to/data.sqlite3 --summary
python3 services/feedback/report.py /path/to/data.sqlite3 --feedback
python3 -m unittest discover -s services/feedback -p 'test_*.py'
```
