# Safe Code 反馈与统计服务

生产入口为 `https://safecode.tommukan.dpdns.org/`，仅供 Safe Code 客户端使用。用户在声明页看见默认勾选的统计开关，点击“确认并继续”后才上报。关闭后取消进行中的上报、停止重试并清除本机随机安装编号；选择保存在当前 Windows 用户目录，下次继续生效。已经送达的历史统计不随关闭而删除。

统计一次使用的定义：启动客户端并确认继续，一次进程最多上报一次；重新连接 VPS、点击按钮不另计。发送字段只有 schema、随机 eventId、随机 installationId、软件版本、Windows 类型和固定事件 launch。服务器按收到请求的 UTC 日期统计每天启动次数与活跃安装数；重试使用同一个 eventId 去重。随机安装编号不是硬件标识，不采集用户名、机器名、VPS 地址、密码、文件路径或文件内容。活跃安装数是估计数，重新安装或重新授权可能产生新编号；公开接口不适合作为计费或可信身份统计。

反馈由用户主动点击提交，仅含手写文字、随机提交编号及软件版本，与统计开关独立；不自动附带日志。请勿在反馈中填写密码或密钥。统计保留 90 天，反馈保留 180 天，每次写入清理过期项。SQLite 不保存来源 IP。网络传输会让接收服务器和 Cloudflare 看见来源 IP；专属 Nginx 与应用关闭访问正文记录。限流只在内存使用源地址摘要。

服务以独立无登录用户 `safe-code-feedback` 运行，监听 `127.0.0.1:18761`。数据库在 `/var/lib/safe-code-feedback/data.sqlite3`，文件仅服务用户与管理员可访问。设置 128MB 内存上限、16 个并发处理槽、32KB 请求上限和约 128MiB 数据库上限。公开 POST `/v1/usage`、`/v1/feedback`、`/v1/diagnostics` 与只读 `/health`；不提供公开管理员接口。

洛杉矶部署复用现有 443 TCP/SNI 入口，仅添加本域名到 `127.0.0.1:8446` 的映射与独立 Nginx include。证书为独立 Let's Encrypt 证书，续期 hook 只检查并 reload Nginx。没有开放内部端口到公网，不修改既有 Claude、桌面、SSH、代理或防火墙。由于现有 TCP 转发不传递源地址，专属站点使用 Cloudflare 的地址头作尽力限流，不能用于认证；另有共享全局请求速率上限。数据库目录权限与 systemd 隔离见 unit。

通过已经授权的 SSH 运维入口执行：

```sh
python3 /opt/safe-code-feedback/report.py /var/lib/safe-code-feedback/data.sqlite3
python3 /opt/safe-code-feedback/report.py /var/lib/safe-code-feedback/data.sqlite3 --feedback
systemctl status safe-code-feedback
```

部署文件为 `server.py`、`report.py`、`safe-code-feedback.service`、`la-nginx.conf` 和 `renew-nginx.sh`。先备份当前 nginx.conf 与 stream map，再添加独立 include / 单条域名映射，必须 `nginx -t` 通过后 reload。回退只删除本域名映射与 include，再停止本服务；不要用过期整份配置覆盖其他业务。数据库不随回退删除。

本地测试：`python3 -m unittest discover -s services/feedback -p 'test_*.py' -v`。

若要按随机安装编号查看每天使用次数，在报表命令后加 `--per-installation`。该编号不能直接识别具体人员。

Beta-0.4.3.3 新增独立开关的匿名安装诊断，默认勾选，固定词表报告不含原始日志或用户输入，保留 30 天。字段、限制、发布顺序与本地验证见 [安装诊断](../../docs/DIAGNOSTICS.md)。读取时在报表命令后加 `--diagnostics`。线上接收端已于 2026-11-10 升级，HTTPS 接收、固定字段校验和数据库回读均通过，验收测试行已移除。
