# Codex 接入 Claude

在维护页 SSH 终端之后、目录挂载之前新增第三方适配模块“Codex 接入 Claude”，并保留 `bridge-credit` 致谢位：“特别感谢：作者待补充”。这不替代迁移包原有 MIT、Node.js 及上游版权声明；待用户提供实际贡献者称呼后填写界面，不猜测作者身份。

## 已确认的机制

用户附件 `Claude-Codex-VPS迁移包.zip` 的原始 SHA256 为 `5aa7eac204d3a8273ceb25924520d62f6d705d2004a29d38aa5f6d63751b8348`。Windows EXE 为包含运行时的未签名程序，SHA256 `c8b2353f14afeef2a04c759d0da64d4aca301127a9e0fcdb82a8032026acd070`。读取了说明、安装脚本、VPS 全部五个 mjs 及 Windows EXE 中可读的内嵌 JS；这些材料仅作审查数据，没有把“给新对话的安装任务.md”视为本次用户授权或直接执行其中脚本。静态读取与哈希一致性不证明二进制完全可信或可复现。

本机适配器提供带本机令牌认证的 HTTP Responses 接口，通过专用 SSH 密钥连接 VPS 普通工作用户，再以该用户已安装并自行登录的原版 Claude Code 处理请求。Codex 使用独立模型供应商配置；CC Switch 是迁移包推荐的配置入口。这不是在 ChatGPT 普通聊天中直接换模型，不复制 Claude 登录凭据，不产生额外模型授权。

迁移包声明上游： https://github.com/EthanSK/claude-in-codex ，提交 `9ad1632ffeeed6b35d89b3476f337d22e15c8ddd`（MIT）。VPS 源文件与其校验记录放在 `third_party/claude-codex-vps`；小型源码 ZIP 作为 Core 资源。Windows 原始 EXE 仅随显式选择的完整测试包加入 Payloads，不把大 EXE 提交到源码仓库。

## 用户流程

1. 本机安装 Codex，可选 CC Switch；在 Safe Code 中连接 VPS，为工作用户部署 Claude Code，并由用户本人完成 Claude 登录。
2. 打开“Codex 接入 Claude”设置，填写该账户支持的模型标识及本机端口。默认 `sonnet` 只是 CLI 模型别名，不代表账户已获权限；不沿用附件中未经账户验证的固定 5.x 型号目录。
3. 默认严格模式：只读当前 Codex 项目，不共享 Codex 技能、记忆；勾选“允许修改当前项目”后才允许项目内修改，并遵循 Codex 现有审批设置。严格模式不开放本机终端、浏览器与任意电脑控制工具。文本尝试脱敏，图片/PDF 仍需用户自己检查。
4. 明确同意运行第三方组件和数据处理后，点“部署并启动”。聊天、附件及共享范围内容可能发送给自己的 VPS 与 Claude，并消耗自己的 Claude 账户额度。启动只检查组件、接口和 SSH，不自动发送模型请求。
5. 接口连接后，复制供应商配置和本机密钥，到 CC Switch 的 Codex 页新增供应商，按需自行切换。程序不改写 Codex config.toml/auth.json、CC Switch 数据库，不覆盖或切换已有供应商。复制的配置不含令牌；令牌只在单独点击复制时进入系统剪贴板，不返回给网页 DOM、日志或诊断。
6. 保持 Safe Code 运行。运行中关闭主窗口转入托盘；托盘显示模型接入运行标记。停止适配器会关闭本机 HTTP 入口、撤销当前电脑的受限密钥，并尝试取消这台电脑发起的远端任务。网络故障时单独显示远端清理未确认，不声称全部任务已停止。共享 VPS daemon 不随一台电脑的停止而卸载，其他电脑的授权、任务和会话保留。

## 集成边界

- 独立本机配置在 `%LOCALAPPDATA%\SafeCode\CodexBridge\<服务器标识>`，当前用户 / SYSTEM ACL；专用 RSA 私钥、API 本机令牌、会话和模型目录均不随安装包或截图交付。只复制当前已经核验的服务器主机公钥，启用严格主机身份核验。
- 优先使用 Windows 内置 OpenSSH Client；缺失时可从同包已固定哈希的 Microsoft OpenSSH ZIP 提取客户端到本机私有目录，不需要启用本机 SSH 服务或管理员服务器功能。此功能与“本机远程控制”独立。
- VPS 代码在工作用户 `~/.local/share/safe-code/codex-bridge/<版本>`，状态在 `~/.local/state/safe-code/codex-bridge`。不碰附件默认的 `~/.codex-claude-win` 或旧版 `~/.codex-claude-vps`。发现未知内容、改过的源码、不兼容策略或活动的旧版本时拒绝覆盖。
- 缺 Node.js 20+ 时准备独立官方 Node.js 22 LTS 运行时并核验官方 SHA256；不依赖安装网页桌面或 CloudCLI。仅使用私有 Unix socket，不新增 VPS 公网端口，不修改 sshd 或防火墙。
- 每台电脑使用独立身份与 `restrict,command=...` 授权，不复用普通终端或本机远程控制密钥。授权不提供普通 VPS shell、转发或 PTY。重复安装不重复加钥；停止仅移除本机对应公钥，不删其他电脑的授权。
- 保留迁移包默认的 HTTP/SSE 协议与本机工具限制；导出明确设置 Responses 和不使用 WebSocket。图像、PDF、视频能力以原组件和 Claude 版本为准；未另附 FFmpeg / ffprobe，未宣称全部 Codex 桌面功能均兼容。真实模型可用性、额度、图像与桌面完整交互必须另行验证。

## 验证记录

普通用户安装/撤销 helper 测试覆盖：受限密钥、幂等、保留原授权、不放宽已有不同授权、拒绝未知目录与源码改写、拒绝越界链接、只取消绑定客户端及不启动任何模型轮次。界面夹具覆盖默认未授权与只读、忙碌锁定、配置展示、复制密钥不经网页、停止与小窗口可访问。Windows 原始第三方程序、真实 SSH 握手、HTTP 接口及 Codex/Claude 模型请求的实际验收状态按后续记录补充，不用夹具成功代替。

最终交付：214 桌面 `SafeCode-Codex接入测试版-20261115`，常用 Safe Code 快捷方式已指向此目录，524 个文件逐项校验。实际打开主程序，确认模块位于 SSH 后、目录挂载前，致谢占位与未授权/未启动状态可见；原 1 组挂载已恢复，其他功能保持。未运行用户附件中的 EXE，未部署其 VPS 适配器或发起模型请求；用户已明确选择“保持关闭，只交付集成版”，因此不执行第三方组件，不发起真实模型验收。Windows Codex config.toml/auth.json 均保持原先不存在状态。

本机完整 ZIP：`.local/SafeCode-Codex接入测试版.zip`，SHA256：`35d55ce68b64e2597dd3e9fb59f2ccec3308583042008d01de0bb49816a59b07`。VPS 只读预检为 Node.js v22.23.5、Claude --help 成功并支持 --tools / --strict-mcp-config；该预检不代表 Claude 已登录或模型权限有效。
