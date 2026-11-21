# Safe Code API

Safe Code 的 Claude Desktop 网关通过同一台 VPS 上的 CloudCLI 处理对话。CloudCLI 负责调用 Claude Code。Safe Code 只传送当前任务、会话信息及已授权的设备和目录说明。

在 Safe Code 通过 SSH 登录 VPS 后，打开主窗口底部的「公网网关」。首次部署时，先安装并打开 CloudCLI，建立网页登录账户，再在 CloudCLI 设置中创建 API Key。在 Safe Code 的网关面板填写这份 CloudCLI API Key 和网页登录资料，点击「安装 API 服务」或「升级 CloudCLI 网关」。Safe Code 用不含任务的请求验证 CloudCLI 连接，验证成功后才启动网关。使用公网 IP 与当前网关端口签发 Safe Code Key，无需域名。

首次安装的网关仍需明确同意「保证自用」才能启用公网。升级时会保留原监听端口、TLS 证书、Safe Code Key、用量及公网启用状态。旧版本的原生网关需要从新版 Safe Code 升级后才能用新版 Key 接入。两种 Key 的用途不同，CloudCLI API Key 只用于同一台 VPS 上的后端连接，Safe Code Key 才填写到设备的「API 接入」。

公网直连的 CloudCLI 网关使用 Desktop 工具审批。带有固定工作站配置的网关继续通过原有回环监听和受信任的 HTTPS 入口运行。可查看 [CloudCLI 工作区与设备控制配置](../../docs/CLOUDCLI_GATEWAY.md)。

用量分析可切换近 1 小时、24 小时和 7 天，查看请求与 Token 吞吐、处理和排队耗时分布、请求结果以及错误与限流趋势。并发与排队显示实时状态，模型用量显示历史累计值。图表使用网关已有的请求日志，明细保留 30 天，列表可查看最近 30 条。请求日志只保存模型、密钥 ID 等统计字段。

新部署的 Safe Code API 处理槽位默认为 0，表示不限制同时处理的 Messages 请求数。网关的连接保护和密钥限额独立生效。管理员可以在「网关总览 → 处理槽位」保存新的上限，所有密钥共享槽位。调低上限时，已经开始的请求继续处理。其余请求按到达顺序排队，最多 12 个，同一个 Key 最多等候 2 个，最长等候 45 秒。满队、超时或超出每分钟限制时返回 429。限频统计滚动 60 秒内获准执行的请求，0 表示不限频，服务重启会重置频率窗口。Claude Desktop 等待工具结果时，不占据 HTTP 请求队列。队列设计参考 [sub2api 的并发服务](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/concurrency_service.go) 中的名额与等待上限。管理只经已连接的 SSH 调用 Unix 套接字，公网端口仅接受模型查询和 Messages，停止本机 SSH 桥接也不会停用独立的 Safe Code API。

网关按 [Claude API 官方标准价](https://platform.claude.com/docs/en/about-claude/pricing) 估算输入、输出、缓存读取和缓存写入费用。缺少缓存时长时按 5 分钟写入价估算。历史数据用当前标准价补算，Haiku 5.5 的历史汇总按基础价估算，因为已有记录缺少每次请求的输入明细。费用栏显示已估算的金额。该金额用于网关额度管理，Claude 订阅和实际账单由服务商单独计算。

创建或编辑密钥时可填写累计美元额度，留空不限额。余额等于累计额度减已用金额。请求完成后扣除实际观测用量的估算费用，余额用完则后续 Messages 请求返回 HTTP 402。一笔在余额为正时开始的请求可能使余额变为负数。提高累计额度即可补充余额。

手动安装时，将本目录的 `install.sh`、`server.py`、`management.py`、`claude_usage.py`、`pricing.py`、`configure_cloudcli.py`、`host_control_mcp.py`、`device_bridge.py`，以及 `src/SafeCode.Core/Remote/` 中的 `desktop_gateway.py` 和 `codex_bridge.zip` 放在同一目录。将示例中的账户名、监听地址和端口替换为你的配置，再以管理员身份运行以下命令。

```sh
bash install.sh workspace 192.0.2.10 45143
systemctl status safe-code-desktop-api
```

在客户端确认「保证自用」并启用公网后，也可在 Linux 工作账户下签发一份 Key。用实际可达的 HTTPS 地址替换示例域名，签发命令会将 Key 写入指定的新文件，并输出可用于撤销的 Key ID。通过私密渠道将文件交给设备的使用者。

```sh
state="$HOME/.local/state/safe-code-desktop-api"
python3 /opt/safe-code/desktop-api/server.py --state "$state" issue https://gateway.example.com:45143 "$state/laptop.key"
```

在新设备上安装官方 Claude Desktop，打开 SafeCode 登录页的「Safe Code API 接入」，粘贴整行 Key，点击「验证并启用」。首次启用后完全退出并重开 Claude Desktop。聊天时保持 SafeCode 运行，再次启动 SafeCode 会恢复本机接入。在同一入口停用可恢复之前选用的 Claude 配置。

Windows 会使用 Claude Desktop 的 3P 策略指向本机转发，并通过 Windows 应用入口启动 Claude Desktop。策略中使用本机转发凭据，公网 Key 仍保存在 Safe Code 的加密状态中。停用接入时，Safe Code 撤销自己写入且仍未被修改的策略字段。

新签发的接入凭证使用加密的 `sc2.` 格式，需要支持该格式的客户端。新版客户端仍接受以前签发的 `sc1.` 凭证。加解密材料随软件分发，完整接入凭证仍需要按密钥保管。

需要撤销时，在网关管理窗口操作，或在 Linux 工作账户下使用签发时输出的 ID。撤销对后续请求生效。已撤销的密钥可在管理窗口彻底删除，相关用量和请求记录也会一并删除。若更换 TLS 证书或公网地址，请为设备签发新 Key。

```sh
python3 /opt/safe-code/desktop-api/server.py --state "$state" revoke "$key_id"
```
### Key 模式的设备控制

网关预留了 Key 模式设备配对接口。仅在服务器配置中启用私有 FRP 监听，并为指定 Key 开通设备配对后可用。旧客户端能继续使用现有接入。支持设备配对的客户端在验证 Key 后应打开原有「本机远程控制」授权窗口，验证远端能通过 SSH 登录本机之后才保存 Claude Desktop 接入配置。用户关闭授权窗口时应结束待配对状态并返回主界面。

独立 frps 实例应仅允许受信任的传输入口访问。将代理监听设为回环，强制 TLS，并将 `Login`、`NewProxy`、`Ping`、`NewWorkConn` 和 `NewUserConn` 接入 SafeCode 的设备授权回调。回调检查设备凭据有效期、所属 Key，以及代理类型、别名和端口。网关为每台设备分配单独的 SSH 身份和项目目录。选中同名目录的设备用不同的 SSH 别名作为项目名称后缀。设备证明只随 SafeCode 发往网关的请求传输。
