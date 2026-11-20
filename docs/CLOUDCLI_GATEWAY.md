# Claude Desktop 经 CloudCLI 访问 Mac

CloudCLI 模式把 Claude Desktop 当作聊天界面。SafeCode 收到 Messages 请求后，从最后一条用户消息提取任务，加入简短的设备和工作目录说明，再以 CloudCLI Key 调用同一台 VPS 上的 `POST /api/agent`。CloudCLI 自己启动 Claude Code，构建原生系统提示并加载项目工具。SafeCode 针对每个网关客户端保存 CloudCLI 会话关联，后续消息继续同一会话。

Claude Desktop 的系统提示、客户端工具定义、模型输出预算、全量历史和授权资料不随这条请求转发。CloudCLI 的回复转成 Messages JSON 或 SSE。流式请求在运行期间显示助手文字、思考状态、工具状态和有限的终端输出。进度以普通文字显示，工具执行和取消仍由 CloudCLI 管理。回传原生思考块需要 CloudCLI 提供 Messages 协议要求的签名。分词、文件附件和 Desktop 工具回传暂未接入。

Desktop 在某个目录第一次发起对话时，SafeCode 用请求中的工作目录识别项目，把对应的 VPS 项目路径交给 CloudCLI Agent API。初始项目名取目录名。同一目录的新对话共用项目，各自保存会话。末级名称相同的不同目录也分别归档。主工作区沿用已配置的 CloudCLI 项目。通过新版配置工具绑定后，未提供可识别目录的新会话归入 `Desktop Chat`，该项目只接收对话。续聊使用已保存的项目路径。

## 准备

需要 VPS 上的 CloudCLI 1.37.7 或 1.37.8。Mac 通过 SafeCode 已启用的「本机远程控制」连接到同一台 VPS。Mac 需有可用的 `/usr/bin/python3`，且 macOS 已允许该 SSH 登录账户访问所选目录。CloudCLI 用的是工作用户配置的 Claude 认证。面向用户的服务使用 Claude Console API Key 或支持的云提供商认证。[Anthropic 认证说明](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)

更新 VPS 网关时，把本仓库 `tools/desktop-api/` 下的安装脚本及其 Python 文件，与 `src/SafeCode.Core/Remote/` 下的 `desktop_gateway.py`、`codex_bridge.zip` 放到同一目录。用 [网关安装脚本](../tools/desktop-api/install.sh) 将监听地址设为 `127.0.0.1`。CloudCLI 模式通过 SafeCode 的 SSH 接入或一条既有的本机回环转发使用，公网监听不承载这个模式。

在 Mac 启用「本机远程控制」，然后用 CloudCLI 服务的 Linux 工作账户运行配置工具。以下目录是公开示例，替换为目标 Mac 上的目录：

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py \
  --workspace /Users/example/Desktop \
  --device-name "MacBook Air" \
  --model claude-haiku-5-5 --model claude-opus-5-5
```

提示出现后粘贴 CloudCLI 设置页生成的 Key。配置程序自动复用唯一可用的 SafeCode 控制身份。有多个连接时，可用 `--client-id` 指定已授权的连接。默认创建独立的 VPS 项目目录，`--project` 可以指定已有项目。若要把 CloudCLI 项目代理限制在专用工作目录和账户，请使用专用 CloudCLI 工作账户和独立项目。CloudCLI 自带的 Agent API 在 VPS 项目内运行工具时启用 `bypassPermissions`。[Agent 路由源码](https://github.com/siteboon/claudecodeui/blob/7e7452219d788847b607d369728085995db719f1/server/modules/agent/agent.routes.ts#L1044-L1051)

配置完成后，重新开启 SafeCode 的 Claude Desktop SSH 接入。使用独立 SafeCode API 服务时，还要重启该服务。准备阶段会通过已授权通道检查一次目标目录的读取权限，不发模型消息。CloudCLI 模式启用后，CloudCLI 或 Mac 控制连接出错会直接返回失败。

## Mac 文件权限

CloudCLI 项目中的 `safecode_host` MCP 工具通过原有通道操作选定目录。可以列出目录、读取及新建 UTF-8 文本文件，以及移动文件或文件夹。移动不覆盖已有目标，路径遍历和符号链接跨目录会被拒绝。CloudCLI 原生文件工具运行在 VPS，因此处理 Mac 文件时应选用这一组工具。已有「本机远程控制」可用来管理更广的设备操作。

用 `--workspace` 指定主工作区，用 `--allow-workspace` 添加其它可访问的目录。SafeCode 按会话的实际选择将文件工具限定在对应文件夹。授权范围之外的目录可以归档对话。检查工作区或在后台保活时无需发送模型消息。

自用部署可在配置时加上 `--allow-ssh-shell`。网关会从现有的 SafeCode 本机远程控制配置读取 SSH 命令，并在已授权项目的任务说明中提供给 CloudCLI。该模式取消 SafeCode 为这些项目注册的文件工具。通过 SSH 执行的命令使用 Mac 登录账户的权限。

要移除此绑定，以同一个 VPS 工作账户运行以下命令，再重启网关或重新开启 SSH 接入：

```sh
/usr/bin/python3 /opt/safe-code/desktop-api/configure_cloudcli.py --disable
```
