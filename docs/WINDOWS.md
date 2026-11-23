# Safe Code for Windows

## 安装

运行发布页中的 `SafeCode-版本-Setup-x64.exe`。支持 Windows 10 2004（19041）及更新版本、Windows 11 x64。安装到当前用户的应用目录，默认创建桌面和开始菜单快捷方式。安装包自带 .NET 和 WebView2 离线运行时。[下载 Windows 安装包](https://github.com/Muchuaner/safe-code/releases)

## 连接和部署

1. 填写 Ubuntu VPS 的 SSH 地址、端口和管理员登录信息。默认 SSH 端口为 22，也可以在「高级连接」修改。首次连接，或服务器重装后指纹变化时，请先核对指纹再确认。
2. 部署要求 Ubuntu 22.04 或 24.04 x86_64、可用的 systemd，以及 root 或具备免密 sudo 的登录账户。建议至少 4 GB 内存和 8 GB 可用磁盘。
3. 分别在「自定义」中保存工作账户与本地化配置，再选择组件并开始部署。安装任务在服务器上继续运行，客户端重新连接后可查看进度。

在维护模式中可以管理工作账户、打开 SSH 终端、连接远程桌面或启动 CloudCLI。CloudCLI 首次打开时，请自行创建网页账户，并按需要用自己的账号登录 Claude Code。SSH 终端依赖 Windows OpenSSH Client，精简系统可在系统的可选功能中启用。

登录资料按当前 Windows 账户加密保存。若在服务器端修改过 SSH 登录密码，回到主界面点击「更新登录密码」，验证同一服务器身份和新密码后，可恢复已配置的通道。

## 远程桌面和目录挂载

远程桌面通过已验证的 SSH 隧道连接。可选择原始大小、等比缩放、动态分辨率或全屏。全屏时把鼠标移到顶部可打开工具栏，也可按 `Ctrl+Alt+Enter` 退出全屏。

要把 Windows 截图贴进云桌面，先点云端文档或聊天输入框，在本机用 `Win+Shift+S` 截图，再点远程桌面工具栏的「粘贴本机图片」。按钮右侧的箭头可以为当前窗口开启「Ctrl+V 粘贴本机图片」，重新打开窗口时该选项默认关闭。文字、文件和富文本继续使用远程桌面的常规粘贴。

图片通过当前桌面的 SSH 连接交给 XFCE 剪贴板。程序优先提供本机已有的图片格式，Windows 截图只提供位图时会生成 PNG。接收应用选择它支持的格式，单次传输的各格式合计最多 32 MiB。

目录挂载支持从本机选择文件夹，并在服务器端选择工作账户下的新目录或空目录。挂载使用 SSH 传输。服务器需预留足够的文件缓存空间，使用期间请保持 Safe Code 运行。退出前在托盘中选择「安全退出」，等待同步和卸载完成。

## Claude Desktop 接入


通过 SSH 登录 VPS 后，在维护模式打开 Claude Desktop 的「网关设置」，点击「启用」，再进入「管理网关」创建 API Key。

回到 Safe Code 主界面或登录页，在「API Key 接入」粘贴完整密钥，点击「验证并启用」。已保存的 Key 可以更换、重新启用或停用。首次接入或修改配置后，请完全退出并重新打开 Claude Desktop。聊天期间请保持 Safe Code 运行。[网关与 Key 使用说明](https://github.com/Muchuaner/safe-code/blob/main/tools/desktop-api/README.md)

如果 Windows 拒绝写入 Claude Desktop 的当前用户策略，请先安全退出 Safe Code，再用同一个 Windows 账户右键 Safe Code，选择「以管理员身份运行」后重试。若电脑由组织管理，请联系管理员确认策略权限。

## 更新和反馈

打开「更新与反馈」可检查版本、提交反馈，以及选择是否启用基础使用统计和匿名功能诊断。这两项默认关闭。[在线更新](https://github.com/Muchuaner/safe-code/blob/main/docs/UPDATES.md) · [匿名功能诊断](https://github.com/Muchuaner/safe-code/blob/main/docs/DIAGNOSTICS.md)
