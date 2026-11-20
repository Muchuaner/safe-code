# Safe Code 反馈服务

这一目录提供匿名使用统计、诊断和手动反馈的接收与查询。客户端的采集范围和保留期见 [匿名功能诊断](../../docs/DIAGNOSTICS.md)。

服务使用 Python 标准库和 SQLite。`server.py` 默认监听本机回环地址。运行时通过 `SAFECODE_PORT` 与 `SAFECODE_DB` 指定端口和数据库文件。部署时可参考同目录的 systemd 单元，并用自己的 HTTPS 反向代理对外提供 `/health`、`/v1/usage`、`/v1/feedback` 和 `/v1/diagnostics`。为接收进程设置单独账户，并确保数据库仅服务账户和管理员可读。

统计和诊断请求采用固定字段校验。查询和汇总在部署主机本地执行，接口无需公开数据库。

```sh
python3 services/feedback/report.py /path/to/data.sqlite3 --summary
python3 services/feedback/report.py /path/to/data.sqlite3 --feedback
python3 -m unittest discover -s services/feedback -p 'test_*.py'
```
