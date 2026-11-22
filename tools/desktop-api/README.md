# Safe Code API

Safe Code 的 Claude Desktop 网关通过同一台 VPS 上的 CloudCLI 处理对话。CloudCLI 负责调用 Claude Code。Safe Code 只传送当前任务、会话信息及已授权的设备和目录说明。

在 Safe Code 通过 SSH 登录 VPS 后，打开「公网网关」，点击「启用」。Safe Code 会检查同一台 VPS 上的 CloudCLI，按需安装或升级网关，并启用公网。首次注册 CloudCLI 时，Safe Code 会自动创建后端 API Key，并显示生成的网页账号和密码。已有 CloudCLI 账户但尚未配置网关时，在「启用」窗口填入 CloudCLI 设置中生成的 API Key。验证连接不发送模型任务。网关使用公网 IP 与当前端口签发 Safe Code API Key。

公网启用后，从「管理网关」创建 Safe Code API Key，再回到主界面「API Key 接入」粘贴。升级会保留原监听端口、TLS 证书、Safe Code API Key、用量及公网启用状态。

在「网关总览 → 前置审查」选择内置 Claude Sonnet、Opus 或独立的 DeepSeek Flash，打开「安全审查」并保存。安全审查首次安装默认关闭，升级保留已有选项。网关 revision 10 新增默认关闭的「语言替换」。开启后，所选模型会将通过安全审查的本轮提示词翻译成英文，再交给 CloudCLI。 [前置审查机制](../../docs/PROMPT_REVIEW.md)

自动创建账户的网关支持 Desktop 工具审批。只配置 CloudCLI API Key 时，对话走 CloudCLI Agent API，附件、计划确认和交互式工具审批需要 CloudCLI 的网页授权。公网 Key 仅供自用。带有固定工作站配置的网关继续通过原有回环监听和受信任的 HTTPS 入口运行。可查看 [CloudCLI 工作区与设备控制配置](../../docs/CLOUDCLI_GATEWAY.md)。

用量分析可切换近 1 小时、24 小时和 7 天，查看请求与 Token 吞吐、处理和排队耗时分布、请求结果以及错误与限流趋势。并发与排队显示实时状态，模型用量显示历史累计值。图表使用网关已有的请求日志，明细保留 30 天，列表可查看最近 30 条。请求日志只保存模型、密钥 ID 等统计字段。

新部署的 Safe Code API 处理槽位默认为 0，表示不限制同时处理的 Messages 请求数。网关的连接保护和密钥限额独立生效。管理员可以在「网关总览 → 处理槽位」保存新的上限，所有密钥共享槽位。调低上限时，已经开始的请求继续处理。其余请求按到达顺序排队，最多 12 个，同一个 Key 最多等候 2 个，最长等候 45 秒。满队、超时或超出每分钟限制时返回 429。限频统计滚动 60 秒内获准执行的请求，0 表示不限频，服务重启会重置频率窗口。Claude Desktop 等待工具结果时，不占据 HTTP 请求队列。队列设计参考 [sub2api 的并发服务](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/concurrency_service.go) 中的名额与等待上限。管理只经已连接的 SSH 调用 Unix 套接字，公网端口提供模型查询、Messages 和仅限本人 Key 的余额查询。

网关按 [Claude API 官方标准价](https://platform.claude.com/docs/en/about-claude/pricing) 估算输入、输出、缓存读取和缓存写入费用。缺少缓存时长时按 5 分钟写入价估算。历史数据用当前标准价补算，Haiku 5.5 的历史汇总按基础价估算，因为已有记录缺少每次请求的输入明细。费用栏显示已估算的金额。该金额用于网关额度管理，Claude 订阅和实际账单由服务商单独计算。使用 API Key 接入的设备可以点击系统状态栏或 Windows 托盘图标，查看该 Key 的已用金额和网关余额。

创建或编辑密钥时可填写累计美元额度，留空不限额。余额等于累计额度减已用金额。请求完成后扣除实际观测用量的估算费用，余额用完则后续 Messages 请求返回 HTTP 402。一笔在余额为正时开始的请求可能使余额变为负数。提高累计额度即可补充余额。

手动安装时，将本目录的 `install.sh`、`server.py`、`management.py`、`claude_usage.py`、`pricing.py`、`configure_cloudcli.py`、`host_control_mcp.py`、`device_bridge.py`、`prompt_review.py`，以及 `src/SafeCode.Core/Remote/` 中的 `desktop_gateway.py` 和 `codex_bridge.zip` 放在同一目录。将示例中的账户名、监听地址和端口替换为你的配置，再以管理员身份运行以下命令。

```sh
bash install.sh workspace 192.0.2.10 45143
systemctl status safe-code-desktop-api
```

在客户端启用公网网关后，也可在 Linux 工作账户下签发一份 Key。用实际可达的 HTTPS 地址替换示例域名，签发命令会将 Key 写入指定的新文件，并输出可用于撤销的 Key ID。通过私密渠道将文件交给设备的使用者。

在「网关管理 → 密钥管理」创建或编辑密钥时，可以为单把 Key 选择允许的 CloudCLI 模型和失效时间。新密钥及升级前签发的密钥默认允许全部模型、永不过期。开启模型限制后，网关只返回获准的模型，并在请求执行前校验模型。选择 CloudCLI 的 Default、Best 等自动选项时，由 CloudCLI 决定实际模型。到期后，网关拒绝这把 Key 的新请求和仍在排队的请求。可以编辑到期的 Key 延长有效期，接入设备可继续使用原 Key。

```sh
state="$HOME/.local/state/safe-code-desktop-api"
python3 /opt/safe-code/desktop-api/server.py --state "$state" issue https://gateway.example.com:45143 "$state/laptop.key"
```

在新设备上安装官方 Claude Desktop，打开 SafeCode 登录页的「API Key 接入」，粘贴整行 Key，点击「验证并启用」。首次启用后完全退出并重开 Claude Desktop。聊天时保持 SafeCode 运行，再次启动 SafeCode 会恢复本机接入。在同一入口停用可恢复之前选用的 Claude 配置。

自动配置的网关读取 CloudCLI 的 Claude 模型目录，包括 Default、Best、Opus Plan、自定义模型和目录中开放的 1M 选项。网关只有 CloudCLI Agent API Key 时，模型目录来自 VPS 工作账户的 Claude Code。手动配置的网关使用管理员设置的模型及 1M 范围。升级后，已接入的设备可在「API Key 接入」点击「刷新模型」，再重启 Claude Desktop。

CloudCLI 提供 Ultracode 时，Claude Desktop 的模型菜单会显示对应的「模型名 · Ultracode」选项。选择它会把 Ultracode 明确传给 CloudCLI。当前 Claude Desktop 的本地开关可能只发送 Extra 档位，无法把远端工作流模式与普通 Extra 区分开。

Windows 会使用 Claude Desktop 的 3P 策略指向本机转发，并通过 Windows 应用入口启动 Claude Desktop。策略中使用本机转发凭据，公网 Key 仍保存在 Safe Code 的加密状态中。停用接入时，Safe Code 撤销自己写入且仍未被修改的策略字段。

新签发的接入凭证使用加密的 `sc2.` 格式，需要支持该格式的客户端。新版客户端仍接受以前签发的 `sc1.` 凭证。加解密材料随软件分发，完整接入凭证仍需要按密钥保管。

需要撤销时，在网关管理窗口操作，或在 Linux 工作账户下使用签发时输出的 ID。撤销对后续请求生效。已撤销的密钥可在管理窗口彻底删除，相关用量和请求记录也会一并删除。若更换 TLS 证书或公网地址，请为设备签发新 Key。

```sh
python3 /opt/safe-code/desktop-api/server.py --state "$state" revoke "$key_id"
```
### Key 模式的设备控制

网关预留了 Key 模式设备配对接口。仅在服务器配置中启用私有 FRP 监听，并为指定 Key 开通设备配对后可用。旧客户端能继续使用现有接入。支持设备配对的客户端在验证 Key 后应打开原有「本机远程控制」授权窗口，验证远端能通过 SSH 登录本机之后才保存 Claude Desktop 接入配置。用户关闭授权窗口时应结束待配对状态并返回主界面。

独立 frps 实例应仅允许受信任的传输入口访问。将代理监听设为回环，强制 TLS，并将 `Login`、`NewProxy`、`Ping`、`NewWorkConn` 和 `NewUserConn` 接入 SafeCode 的设备授权回调。回调检查设备凭据有效期、所属 Key，以及代理类型、别名和端口。网关为每台设备分配单独的 SSH 身份和项目目录。选中同名目录的设备用不同的 SSH 别名作为项目名称后缀。设备证明只随 SafeCode 发往网关的请求传输。
