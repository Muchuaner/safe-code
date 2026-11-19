# Safe Code API

Linux 服务以现有 Claude Code 工作账户运行。FRP 用 TCP 转发到安装时指定的 Linux 地址和端口，TLS 在 Linux 服务上终止。214 的公网入口为 `frp-egg.com:45143`，内网目标为 `192.168.50.66:45143`。每个新设备可以单独签发一份 Key，方便日后撤销。

公网调用默认关闭，本机 Claude Desktop 仍可使用 SSH 桥接。在连接 VPS 的 Safe Code 客户端，进入「维护模式 → 本地 Claude Desktop 接入 → 设置 → 公网启用」，阅读警告后点击「保证自用」才会启用 VPS 的独立 API。启用状态保存在该 VPS 的工作账户中，重启后仍有效。此前签发的密钥在确认后恢复可用。随后点击「管理网关」，在独立窗口搜索、创建、修改或撤销密钥，并查看逐把密钥的请求和用量。完整密钥仅在创建时显示。

客户端确认 VPS 已开通公网后，会记住该 SSH 账户和已验证的主机指纹。下次启动时，保存的登录信息可用就自动恢复管理连接。网络短暂中断会自动重连。若服务器身份变化或登录密码失效，需要核对后继续。主窗口底部显示公网并发和排队，网关总览同时显示 VPS 上 Claude 订阅的 5 小时与 7 天剩余额度。订阅用量由 VPS 向 Anthropic 查询并缓存五分钟，密钥和订阅登录凭据均不会出现在监控响应中。

「网关总览 → 网关设置」显示 Linux 网关实际监听的 IP 和端口。直接监听公网 IP 时显示「公网 IP」。如果 VPS 的公网地址由云平台映射，可在安装时用第四个参数传入该公网 IP，窗口会同时显示公网 IP 和本地监听 IP。214 虚拟机使用本地监听地址。保存新端口会重启服务，启动失败时尝试恢复原端口。若使用 FRP，请同步更新其目标端口。若外部访问地址或端口也变更，请用新的 HTTPS 地址重新签发密钥。

首次签发前，在「网关设置」中保存公网 HTTPS 地址。创建密钥只需填写备注和每分钟请求上限，网关会将已保存的公网地址写入接入凭证。保存公网地址本身无需重启服务。

214 测试虚拟机通过主机名 `tom666` 和监听 IP `192.168.50.66` 匹配开发者渠道。网关设置显示固定的 FRP 入口 `https://frp-egg.com:45143`，该地址自动写入新签发的凭证。公网启用确认和 TLS 证书校验仍然适用。

用量分析可切换近 1 小时、24 小时和 7 天，查看请求与 Token 吞吐、处理和排队耗时分布、请求结果以及错误与限流趋势。并发与排队显示实时状态，模型用量显示历史累计值。图表使用网关已有的请求日志，明细保留 30 天，列表可查看最近 30 条。请求日志只保存模型、密钥 ID 等统计字段。

Safe Code API 默认同时处理一个 Messages 请求。管理员可以在「网关总览 → 处理槽位」保存新的上限，所有密钥共享槽位。调低上限时，已经开始的请求继续处理。其余请求按到达顺序排队，最多 12 个，同一个 Key 最多等候 2 个，最长等候 45 秒。满队、超时或超出每分钟限制时返回 429。限频统计滚动 60 秒内获准执行的请求，0 表示不限频，服务重启会重置频率窗口。Claude Desktop 等待工具结果时，不占据 HTTP 请求队列。队列设计参考 [sub2api 的并发服务](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/concurrency_service.go) 中的名额与等待上限。管理只经已连接的 SSH 调用 Unix 套接字，公网端口仅接受模型查询和 Messages，停止本机 SSH 桥接也不会停用独立的 Safe Code API。

网关按 [Claude API 官方标准价](https://platform.claude.com/docs/en/about-claude/pricing) 估算输入、输出、缓存读取和缓存写入费用。缺少缓存时长时按 5 分钟写入价估算。历史数据用当前标准价补算，Haiku 5.5 的历史汇总按基础价估算，因为已有记录缺少每次请求的输入明细。费用栏显示已估算的金额。该金额用于网关额度管理，Claude 订阅和实际账单由服务商单独计算。

创建或编辑密钥时可填写累计美元额度，留空不限额。余额等于累计额度减已用金额。请求完成后扣除实际观测用量的估算费用，余额用完则后续 Messages 请求返回 HTTP 402。一笔在余额为正时开始的请求可能使余额变为负数。提高累计额度即可补充余额。

部署时将本目录的 `install.sh`、`server.py`、`management.py`、`pricing.py`、项目中的 `src/SafeCode.Core/Remote/desktop_gateway.py` 和该桥接使用的 `codex_bridge.zip` 放到同一目录。以管理员身份运行以下命令。

```sh
bash install.sh workspace 192.168.50.66 45143
systemctl status safe-code-desktop-api
```

在客户端确认「保证自用」并启用公网后，也可在 Linux 工作账户下签发一份 Key。签发命令将 Key 写入指定的新文件，并输出可用于撤销的 Key ID。通过私密渠道将文件交给设备的使用者。

```sh
state="$HOME/.local/state/safe-code-desktop-api"
python3 /opt/safe-code/desktop-api/server.py --state "$state" issue https://frp-egg.com:45143 "$state/laptop.key"
```

在新设备上安装官方 Claude Desktop，打开 SafeCode 登录页的「Safe Code API 接入」，粘贴整行 Key，点击「验证并启用」。首次启用后完全退出并重开 Claude Desktop。聊天时保持 SafeCode 运行，再次启动 SafeCode 会恢复本机接入。在同一入口停用可恢复之前选用的 Claude 配置。

Windows 会使用 Claude Desktop 的 3P 策略指向本机转发，并通过 Windows 应用入口启动 Claude Desktop。策略中使用本机转发凭据，公网 Key 仍保存在 Safe Code 的加密状态中。停用接入时，Safe Code 撤销自己写入且仍未被修改的策略字段。

新签发的接入凭证使用加密的 `sc2.` 格式，需要支持该格式的客户端。新版客户端仍接受以前签发的 `sc1.` 凭证。加解密材料随软件分发，完整接入凭证仍需要按密钥保管。

需要撤销时，在网关管理窗口操作，或在 Linux 工作账户下使用签发时输出的 ID。撤销对后续请求生效。已撤销的密钥可在管理窗口彻底删除，相关用量和请求记录也会一并删除。若更换 TLS 证书或公网地址，请为设备签发新 Key。

```sh
python3 /opt/safe-code/desktop-api/server.py --state "$state" revoke "$key_id"
```
