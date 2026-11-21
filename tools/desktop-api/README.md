# Safe Code API

已有 CloudCLI 和 SafeCode「本机远程控制」的用户可以配置 [CloudCLI 后端](../../docs/CLOUDCLI_GATEWAY.md)，通过 Desktop 发送文本任务并使用指定 Mac 目录的文件工具。

Linux 服务以现有 Claude Code 工作账户运行。可以直连 VPS，也可以使用 TCP 中转连接安装时指定的监听地址，TLS 在 Linux 服务上终止。可以为每台设备单独签发一份 Key，方便日后撤销。

公网调用默认关闭，本机 Claude Desktop 仍可使用 SSH 桥接。在连接 VPS 的 Safe Code 客户端，进入「维护模式 → 本地 Claude Desktop 接入 → 设置 → 公网启用」，阅读警告后点击「保证自用」才会启用 VPS 的独立 API。启用状态保存在该 VPS 的工作账户中，重启后仍有效。此前签发的密钥在确认后恢复可用。随后点击「管理网关」，在独立窗口搜索、创建、修改或撤销密钥，并查看逐把密钥的请求和用量。完整密钥仅在创建时显示。

如果 VPS 尚未安装独立 API，设置窗口会显示「安装 API 服务」。安装复用 VPS 工作账户，需要该账户已安装 Claude Code，服务器有 Node.js 20 或以上。新服务默认使用 45143 端口。使用 CloudCLI 后端时监听回环地址，其他情况监听 IPv4 网卡。安装完成后，仍需单独确认「公网启用」。已有 API 服务继续沿用原安装。

客户端确认 VPS 已开通公网后，会记住该 SSH 账户和已验证的主机指纹。下次启动时，保存的登录信息可用就自动恢复管理连接。网络短暂中断会自动重连。若服务器身份变化或登录密码失效，需要核对后继续。主窗口底部显示公网并发和排队，网关总览同时显示 VPS 上 Claude 订阅的 5 小时与 7 天剩余额度。订阅用量由 VPS 向 Anthropic 查询并缓存五分钟，密钥和订阅登录凭据均不会出现在监控响应中。

「网关总览 → 网关设置」显示 Linux 网关实际监听的 IP 和端口。直接监听公网 IP 时显示「公网 IP」。如果 VPS 的公网地址由云平台映射，可在安装时用第四个参数传入该公网 IP，窗口会同时显示公网 IP 和本地监听 IP。保存新端口会重启服务，启动失败时尝试恢复原端口。若使用 TCP 中转，请同步更新其目标端口。若外部访问地址或端口也变更，请用新的 HTTPS 地址重新签发密钥。

首次签发前，在「网关设置」中保存公网 HTTPS 地址。创建密钥只需填写备注和每分钟请求上限，网关会将已保存的公网地址写入接入凭证。保存公网地址本身无需重启服务。

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
