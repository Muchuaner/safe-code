# Claude Desktop 经 CloudCLI 访问 Mac

## 网关架构约束

API Key 接入的唯一模型调用链路是 **SafeCode 网关 → CloudCLI → Claude Code**。由 CloudCLI 启动 Claude Code。CloudCLI 使用独立发布的上游版本，网关安全改动由 SafeCode 的请求处理、设备授权和部署配置实现。CloudCLI 不可用时，网关直接返回错误。

旧后端的 `DESKTOP_LAUNCHER`、`Channel`、`Conversation` 及 `Manager` 中的直启分支都标记为**待删除**。禁止恢复、扩展或将它们用作兼容回退。网关内直接启动 CLI 的模型目录和订阅用量探测也待迁移并删除。

## 请求链路

CloudCLI 模式把 Claude Desktop 当作聊天界面。SafeCode 收到 Messages 请求后，从最后一条用户消息提取任务，再交给同一台 VPS 上的 CloudCLI。CloudCLI 自己启动 Claude Code，构建原生系统提示并加载项目工具。对于已绑定的本机目录，SafeCode 在会话首轮附加设备、项目根目录和现有执行通道的说明。续聊使用同一 CloudCLI 会话和原有上下文。已有会话升级后会在下一轮补发一次。若编辑历史删掉了携带说明的消息，网关会在替换消息中补回当前说明。

Claude Desktop 的系统提示、客户端工具定义、模型输出预算、全量历史和授权资料不随这条请求转发。SafeCode 在本地处理 Desktop 的网关连通性探测、起名和分支名建议。探测只验证 Desktop 到 SafeCode 的连通性。CloudCLI 用首条消息的开头生成会话标题，因此 SafeCode 将实际任务放在设备说明之前。短任务的标题仍可能带上部分设备说明。

## CloudCLI 功能清单与 Desktop 适配

以下是 CloudCLI 自托管 1.37.8 的功能和 SafeCode 的适配情况。部分功能需要先开启网关与 CloudCLI 的已认证连接。对话流使用 Messages SSE，执行详情可在 CloudCLI 原会话查看。

### 对话与执行

| CloudCLI 功能 | Claude Desktop 适配情况 |
| --- | --- |
| 文本聊天、流式输出和续聊 | **已接通。** 保留助手输出顺序和 CloudCLI 会话绑定，区分完成、上游失败和取消 |
| 项目归类、工作目录和设备隔离 | **已接通。** 按 Desktop 工作目录归到对应的 CloudCLI 项目，同名目录按设备隔离 |
| 会话标题、分支名建议和连通性探测 | **已接通。** 网关本地处理 Desktop 起名与探测，普通对话的 CloudCLI 标题取首条任务开头。建议回复当前返回空结果 |
| 模型目录、同会话切换、推理强度和 1M 上下文 | **已接通。** 自动配置的网关读取 CloudCLI 模型目录及其档位和 1M 选项，手动配置使用管理员指定的模型范围。已认证的会话切换通过 CloudCLI 聊天接口更新同一会话的模型 |
| 思考状态、上下文压缩和截断 | CloudCLI 上报的主助手思考以原生 Messages 思考块显示，压缩和截断以文字提示 |
| 工具执行、子代理、后台任务和工作流 | Desktop 显示简短的运行提示和汇总。完整参数、输出、逐项折叠及停止单个后台任务在 CloudCLI 原会话中处理 |
| 选择题 `AskUserQuestion` | **原生接通。** 单选、多选、自定义答案和跳过回到 CloudCLI 当前待答的执行 |
| 计划模式和 `ExitPlanMode` | **原生接通。** Desktop 进入计划模式后，CloudCLI 也以计划模式执行。网关收到完整计划正文后，先呈现正文，再用 Desktop 的原生确认接收批准或拒绝 |
| 工具审批、记住许可和 MCP 表单 | **部分接通。** 开启一次性授权后，CloudCLI 发来的工具审批会在 Desktop 展示真实参数，并提供允许一次或拒绝。持久许可和 MCP 表单由 CloudCLI 管理 |
| 粘贴截图、图片和上传文档 | **已接通。** 内嵌 PNG、JPEG、GIF、WebP、PDF 和支持的文本附件通过 CloudCLI 上传接口随消息发送。单文件上限 5 MiB，整体受 Messages 请求上限约束 |
| 生成图片和文件的预览、下载 | **部分接通。** 文本输出可见，二进制结果提示在 CloudCLI 查看 |
| 停止生成 | **已接通。** Desktop 关闭正在运行的流后，网关校验 CloudCLI 的会话与本轮启动时间，再发送取消。收到确认后保留续聊关联。等待选择框或计划确认时，用界面里的拒绝或跳过操作 |
| 编辑已发送消息后重发 | **已接通。** 同一 Desktop 会话可通过已验证的 CloudCLI 消息锚点回到编辑位置。启用后记录的新消息可映射锚点。远端历史发生变化时会提示冲突 |
| 分支和运行中插话 | **尚未接入。** 分支需要可靠的父会话信息。真正的插话需要 Desktop 传出区别于排队发送的动作 |
| Token 用量、上下文窗口和预先分词 | **部分接通。** 已展示运行后用量及上下文窗口。精确分词仍需提供方专用接口 |

### CloudCLI 工作区和管理功能

| CloudCLI 功能 | Claude Desktop 适配情况 |
| --- | --- |
| 项目列表、收藏、归档、恢复、重命名 | 网关负责项目归类。这些管理操作在 CloudCLI |
| 历史发现、检索、归档、恢复、导出和会话复制 | Desktop 发起的对话可继续原 CloudCLI 会话。历史库、搜索和导出各自由客户端管理 |
| 会话内侧边提问、草稿、队列和定时消息 | CloudCLI 管理。Desktop 目前接入主会话的同步响应 |
| 终端、文件树、编辑器和文件管理 | 模型可通过已授权的 SafeCode 本机控制操作选定目录。CloudCLI 的交互终端和文件面板仍在 CloudCLI |
| Git 状态、差异、提交、分支、Worktree 和 GitHub 操作 | CloudCLI 管理界面提供这些功能。对话里能完成的命令取决于运行账户和工具权限 |
| Claude Code 斜杠命令、Skills、MCP 和项目记忆文件 | CloudCLI 运行时按 VPS 的项目和用户配置使用。Desktop 自己的菜单、插件和账号记忆各有配置 |
| 浏览器会话、语音转写和朗读 | CloudCLI 自带界面提供，取决于已启用的运行时与服务 |
| 通知、Web Push、Webhook 和用户偏好 | CloudCLI 管理。Desktop 收到的是当前对话的流式事件 |
| Cursor、Codex、OpenCode、多端界面、CloudCLI 插件和可选 TaskMaster | CloudCLI 功能。SafeCode 的 Claude Desktop 接入使用 Claude Code 提供方 |
| 账户登录、凭据、实例配置和 CLI 更新 | 由 CloudCLI 管理。Claude 官方账号的跨设备历史与账号偏好仍由 Claude 账户服务管理 |

启用原生选择题后，支持选择题的 Desktop 会话通过 CloudCLI 已认证的聊天接口运行，纯文本消息也可以弹出选择框。附件与计划模式同样使用这个接口。其他普通纯文本请求继续使用 CloudCLI 的 Agent API。SafeCode 利用 CloudCLI 的已认证控制通道提交选择、计划确认及停止指令。审批需要和同一轮里的待处理请求匹配，附件保存在 CloudCLI 的上传目录。

在 SafeCode 点击「打开 Claude Desktop」时，客户端会核对当前接入配置，并通过本机转发器查询网关的模型目录。若 Claude 早于本次接入启动，请完全退出后再打开。Windows 支持已注册的 Claude 应用和带 Anthropic 签名的独立桌面程序，macOS 按应用标识启动。未找到应用时会打开[Claude Desktop 官方下载页](https://claude.com/download)，安装后可以回到 SafeCode 重试。

仅配置 CloudCLI Agent API Key 的网关会在 Desktop 断线后继续接收这一轮的结果。重新连接时，如果任务仍在运行，请等待或在 CloudCLI 中停止它。完成后，在原对话重试可以取回结果，网关不会重复提交任务。截图、附件、计划模式和从 Desktop 停止云端任务需要管理员先连接已有的 CloudCLI 网页账户，配置方法见下文。

## 准备

需要 VPS 上的 CloudCLI 1.37.7 或 1.37.8。Mac 通过 SafeCode 已启用的「本机远程控制」连接到同一台 VPS。Mac 需有可用的 `/usr/bin/python3`，且 macOS 已允许该 SSH 登录账户访问所选目录。CloudCLI 用的是工作用户配置的 Claude 认证。面向用户的服务使用 Claude Console API Key 或支持的云提供商认证。[Anthropic 认证说明](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)

更新 VPS 网关时，把本仓库 `tools/desktop-api/` 下的安装脚本及其 Python 文件，与 `src/SafeCode.Core/Remote/` 下的 `desktop_gateway.py`、`codex_bridge.zip` 放到同一目录。已有固定工作站配置的网关，使用 [网关安装脚本](../tools/desktop-api/install.sh) 保持监听地址为 `127.0.0.1`，继续使用原 HTTPS 入口或 SSH 通道。新部署的公网直连网关可通过 Safe Code 的「公网网关」安装和授权。自动创建账户的网关使用 CloudCLI 的标准工具审批。

在 Mac 启用「本机远程控制」，然后用 CloudCLI 服务的 Linux 工作账户运行配置工具。以下目录是公开示例，替换为目标 Mac 上的目录：

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py \
  --workspace /Users/example/Desktop \
  --device-name "MacBook Air" \
  --model claude-haiku-5-5 --model claude-opus-5-5
```

提示出现后粘贴 CloudCLI 设置页生成的 Key。配置程序自动复用唯一可用的 SafeCode 控制身份。有多个连接时，可用 `--client-id` 指定已授权的连接。默认创建独立的 VPS 项目目录，`--project` 可以指定已有项目。若要把 CloudCLI 项目代理限制在专用工作目录和账户，请使用专用 CloudCLI 工作账户和独立项目。CloudCLI 自带的 Agent API 在 VPS 项目内运行工具时启用 `bypassPermissions`。[Agent 路由源码](https://github.com/siteboon/claudecodeui/blob/7e7452219d788847b607d369728085995db719f1/server/modules/agent/agent.routes.ts#L1044-L1051)

### 开启 Desktop 的交互和附件

在 VPS 网关使用的 Python 环境安装 `websocket-client`。Debian 和 Ubuntu 使用系统包 `python3-websocket`。已部署的网关可以用同一个工作账户执行以下命令开启原生选择框、计划确认、附件和停止生成。

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py --desktop-questions
```

按提示输入已有的 **CloudCLI 网页账户**。SafeCode 通过本机 CloudCLI 登录接口换取短期会话令牌，登录资料保存在 VPS 工作账户权限为 `0600` 的网关配置中。Desktop 用户照常使用原有 SafeCode API Key。重新加载网关后，用户可以在 Desktop 直接完成选择和计划审批。待答请求最多保留五分钟。计划正文缺失或超过完整呈现上限时，可到 CloudCLI 查看并处理对应请求。用 `--no-desktop-questions` 清除此配置。

使用回环监听的自管网关也可以由服务器管理员提供短期 CloudCLI 会话令牌。将网关配置的 `desktopQuestions` 设为 `{"tokenFile":"/run/safe-code-cloudcli-session/session.jwt"}`。令牌文件由网关工作账户持有，权限为 `0600`，有效期不超过五分钟，由本机受信服务在到期前原子替换。SafeCode 只将它发送到本机 CloudCLI。公网直连模式不接受这项配置。

同会话切换模型和编辑重发也使用此认证。可编辑的消息需要在已启用认证的网关上完成并记录锚点。编辑仅调整对话历史，文件或终端产生的变更按其各自的操作处理。

### API Key 的工具权限

通过 Safe Code API 网关接入时，新签发和已存在的 Key 在普通对话中都使用 CloudCLI 的原生无限制工具模式。为设备配对前，请确认它可以使用该账户的工作目录和工具。计划确认和需要用户选择的问题会继续显示交互卡片。管理员可独立配置网关前置审查。

卡片显示期间切换模型时，提交的选择仍会送给发出该卡片的运行任务。下一条普通消息会使用新选的模型。已结束的提问会提示重新提问。

### 启用 1M 上下文

确定 CloudCLI 登录的 Claude 提供方已经开放该型号的 1M 上下文后，用对应的网关允许列表型号开启。这里使用示例型号。

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py --long-context-model claude-opus-5-5
```

有多个型号时可重复传入 `--long-context-model`。重载网关并刷新 Desktop 的网关接入配置后，已启用型号才会显示 1M 能力。SafeCode 用原型号的 `[1m]` 变体发给 CloudCLI，并在对话中显示 CloudCLI 上报的窗口大小。若回复上报了不同型号，Desktop 会显示型号差异。用 `--clear-long-context-models` 清除 1M 配置。

可通过 `--web-origin https://cloud.example.com` 指定 CloudCLI 网页的 HTTPS 地址，供计划确认、工具审批以及较旧客户端的提示打开原会话。省略该选项时，提示显示会话 ID。

配置完成后，重启 SafeCode API 服务，再使用 API Key 接入 Claude Desktop。准备阶段会通过已授权通道检查一次目标目录的读取权限，不发模型消息。CloudCLI 模式启用后，CloudCLI 或 Mac 控制连接出错会直接返回失败。

## Mac 文件权限

CloudCLI 项目中的 `safecode_host` MCP 工具通过原有通道操作选定目录。可以列出目录、读取及新建 UTF-8 文本文件，以及移动文件或文件夹。移动不覆盖已有目标，路径遍历和符号链接跨目录会被拒绝。CloudCLI 原生文件工具运行在 VPS，因此处理 Mac 文件时应选用这一组工具。已有「本机远程控制」可用来管理更广的设备操作。

用 `--workspace` 指定主工作区，用 `--allow-workspace` 添加其它可访问的目录。SafeCode 按会话的实际选择将文件工具限定在对应文件夹。授权范围之外的目录可以归档对话。检查工作区或在后台保活时无需发送模型消息。

自用部署可在配置时加上 `--allow-ssh-shell`。网关会从现有的 SafeCode 本机远程控制配置读取 SSH 命令，并在已授权项目的任务说明中提供给 CloudCLI。该模式取消 SafeCode 为这些项目注册的文件工具。通过 SSH 执行的命令使用 Mac 登录账户的权限。

要移除此绑定，以同一个 VPS 工作账户运行以下命令，再重启网关或重新开启 SSH 接入：

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py --disable
```
