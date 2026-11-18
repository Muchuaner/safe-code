# Safe Code for macOS

当前修复版：Beta-0.4.4.2；首个开发版为 Beta-0.4.4.1。分别构建 Apple Silicon（arm64）和 Intel（x64）应用，构建目标为 macOS 15 或以后版本。最低系统版本按 [.NET 10 支持列表](https://github.com/dotnet/core/blob/main/release-notes/10.0/supported-os.md) 设定；macOS 14 已不在当前支持范围。实际验收系统、架构与限制见下方验证记录；交叉编译不代表已在 Intel 实机验收。

打开对应 DMG，将 Safe Code.app 拖入 Applications。应用自带 .NET 核心，不需要安装 Python、Node.js、.NET SDK、WebView2 或 Windows 模拟器。系统 WKWebView 承载界面，AppKit 提供窗口、文件夹选择器、剪贴板和菜单栏面板。

这是本地临时签名的开发包，未完成 Apple Developer ID 签名与公证。下载的开发包可能被 Gatekeeper 拦截；只有确认来源后才在系统设置的隐私与安全性页面选择打开，不要关闭系统安全检查。正式分发需使用开发者证书重新签名并提交 Apple 公证。

## 功能

SSH 指纹确认、Ubuntu 部署、工作账户草稿、IP 设置、目录浏览与新建、多目录挂载、大文件传输、断线守护、安全卸载和 CloudCLI 原生项目联动复用现有核心。关闭有任务或挂载的窗口会保留菜单栏守护；菜单栏提供主界面、重连和安全退出。退出前必须确认同步卸载成功。

“SSH 终端”调用系统 Terminal 和 /usr/bin/ssh，使用独立配置、专用密钥和已核验服务器指纹，不把密码写进终端脚本或命令参数。首次使用可能由 macOS 询问是否允许控制 Terminal。

“远程桌面”通过 SSH 连接服务器回环地址的 3389 端口，按服务器身份和工作用户名复用本机回环端口，优先绑定相同端口，端口被占用时由系统分配。通过 RDP 握手确认 xrdp 有回应后，生成不含密码的 .rdp 文件，交给已安装的 Windows App 或默认 RDP 客户端。工作用户密码需在该客户端自行填写；更改分辨率在下次打开时生效。

登录信息和工作账户草稿以 AES-256-GCM 加密保存，加密密钥放在 macOS 登录钥匙串。应用数据位于 ~/Library/Application Support/Safe Code，目录仅当前用户可访问。应用不会读取或迁移 Windows DPAPI 数据，也不会写入 Codex 或 CC Switch 配置。

## 本机远程控制

默认关闭。Mac 版接入 macOS 系统已有的“远程登录”，不自行启用系统服务，不修改 sshd、允许用户列表或防火墙。

使用前，需本人在系统设置 → 通用 → 共享中开启远程登录，并仅允许当前账户。允许后，本功能给当前用户 authorized_keys 添加专用受限公钥，通过已有本机 22 端口建立服务器回环反向通道，核验 Darwin 系统与真实用户 UID。此权限可以运行命令、读写或删除当前用户可访问的文件；macOS 隐私权限和需要密码的 sudo 操作仍由系统控制。

停用会撤销当前专用公钥、断开反向通道并清理服务器专用身份；不会替用户关闭原来启用的系统远程登录。应用异常中断后，再次启动会先撤销自己的残留公钥，不会自动恢复控制。没有显式测试授权时不启用本机控制。

## 第三方模型适配器

用户迁移包仅含 Windows EXE，没有可验证的 macOS 程序。此模块鸣谢“亚撒西の空白”并显示“暂不可用”，不能启动，不尝试用模拟器执行，也不发起真实模型请求。后续需取得经过审查、具有相应分发许可的 macOS 适配器，再独立验收。

## 更新与反馈

Mac 更新只匹配当前架构的 macOS DMG，不会下载 Windows EXE 或另一架构的安装包。下载后核验 GitHub SHA256；用户完成卸载后打开镜像，自行替换应用。没有 Mac 安装资产时只提供版本发布页。

沿用明确同意后的诊断与反馈流程，基础统计系统字段为 macOS。Beta-0.4.4.2 发布时已部署接收端平台兼容更新，Windows/macOS 启动事件均通过验收，用户授权和采集字段不变。

## 构建

```sh
python3 tools/build_macos.py --arch arm64
python3 tools/build_macos.py --arch x64
```

可选 --claude-payload 指向已核对 SHA256 的官方 Linux x64 Claude Code 2.1.311，供服务器安装时通过 SSH 上传；其余架构按现有官方校验下载流程处理。构建脚本只写本地 dist，不发布或替换已安装应用。可用 --sign-identity 指定开发者签名；公证是单独的发行步骤。

## 验证记录（2026-11-15）

Apple Silicon 实机系统为 macOS 26.6.2。原生启动已实际使用本机钥匙串生成加密密钥，并检查 AppKit 菜单、无服务器预览与安全退出。AppKit / WKWebView 的桥接、独立指南确认、标准尺寸维护界面、未连接操作拒绝、只读启动及 Windows 适配器不可用检查通过。Intel x64 包另经 Rosetta 启动验收，不等同于 Intel 硬件实机验收。


AES-GCM 往返及篡改拒绝、Shell 参数引用、平台和架构更新筛选通过。Mac 与 Windows 共用界面、指南、主机身份、目录边界及 CloudCLI 回归共 13 个脚本通过；诊断接收端 17 项测试通过，Windows 客户端仍以零警告、零错误编译。

本次没有开启 macOS 系统远程登录或本机控制，没有运行用户附件适配器，没有发送模型请求，没有切换 Codex / CC Switch 配置。Terminal 自动化授权、RDP 客户端实际登录，以及 Mac 本机控制真实登录需在用户主动使用或另行授权的验收中完成，不用构建或夹具成功代替。

应用使用临时本机签名，已核验签名封装与包内文件哈希；未提交 Apple 公证。构建在 ~/Library/Caches/SafeCode/Builds 下完成，避免同步目录给 .app 自动附加 Finder 元数据而破坏签名；最终 DMG 输出到 dist/macos。上述为首个开发版验收记录；随后 Beta-0.4.4.2 已公开预发布，并完成统计接收端平台兼容更新。

