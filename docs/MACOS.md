# Safe Code for macOS

首个开发版：Beta-0.4.4.1。分别构建 Apple Silicon（arm64）和 Intel（x64）应用，构建目标为 macOS 15 或以后版本。最低系统版本按 [.NET 10 支持列表](https://github.com/dotnet/core/blob/main/release-notes/10.0/supported-os.md) 设定；macOS 14 已不在当前支持范围。实际验收系统、架构与限制见下方验证记录；交叉编译不代表已在 Intel 实机验收。

打开对应 DMG，将 Safe Code.app 拖入 Applications。应用自带 .NET 核心，不需要安装 Python、Node.js、.NET SDK、WebView2 或 Windows 模拟器。系统 WKWebView 承载界面，AppKit 提供窗口、文件夹选择器、剪贴板和菜单栏面板。

这是本地临时签名的开发包，未完成 Apple Developer ID 签名与公证。下载的开发包可能被 Gatekeeper 拦截；只有确认来源后才在系统设置的隐私与安全性页面选择打开，不要关闭系统安全检查。正式分发需使用开发者证书重新签名并提交 Apple 公证。

## 功能

SSH 指纹确认、Ubuntu 部署、工作账户草稿、IP 设置、目录浏览与新建、多目录挂载、大文件传输、断线守护、安全卸载和 CloudCLI 原生项目联动复用现有核心。关闭有任务或挂载的窗口会保留菜单栏守护；菜单栏提供主界面、重连和安全退出。退出前必须确认同步卸载成功。

“SSH 终端”调用系统 Terminal 和 /usr/bin/ssh，使用独立配置、专用密钥和已核验服务器指纹，不把密码写进终端脚本或命令参数。首次使用可能由 macOS 询问是否允许控制 Terminal。

“远程桌面”通过 SSH 连接服务器回环地址的 3389 端口，在本机生成不含密码的 `.rdp` 文件并直接拉起 RDP 客户端。macOS 不自带 RDP 客户端，因此这一步由应用自己收尾，不需要用户先去商店或官网安装：已装 Microsoft Windows App 或 Homebrew FreeRDP 时直接复用，两者都没有时应用自动下载官方安装包（约 90 MB，SHA-256 与 Microsoft Developer ID 双重校验），解包后安装到 `~/Applications`，不需要管理员密码和 Installer 交互。失败时提供重试与下载页两个出口。本地入口固定为 `127.0.0.1:13389`（被占用时回落随机端口），文件固定为 `Desktops/Safe Code.rdp`，同一会话内重复点击复用同一条隧道。地址固定后客户端凭据可长期复用，不必每次当作新机器重新选择。服务端的第二层认证（xrdp 自己的登录窗口）同样不再出现：`安装虚拟桌面` 会写入 `autorun` 与 `[Xorg]` 固定凭据，点一下直接进桌面，机制与安全边界见 `docs/DESKTOP_AUTOLOGIN_2026-11-15.md`。打开前会先探测本地端口，隧道已断时直接提示重新连接，不把失效地址交给客户端。

需要注意的是密码有两层，`.rdp` 只能解决第一层。客户端会把凭据存入 macOS 钥匙串；但 xrdp 在收到连接后会用 PAM 再认证一次，那个带 `Session` 下拉框的登录窗口属于服务端，`.rdp` 文件无法携带密码，`authentication level` 也只控制服务器证书校验而不影响这一步。因此未在服务器启用 `require_credentials` 时，每轮会话仍需要输入一次工作用户密码。要免掉它，需在 `/etc/xrdp/xrdp.ini` 的 `[Globals]` 设置 `require_credentials=true` 并重启 xrdp：xrdp 手册说明该选项会改用连接初始阶段由客户端提供的凭据，不再显示登录窗口；代价是客户端拿不到凭据时会直接认证失败，而不是退回登录窗口。

登录信息和工作账户草稿以 AES-256-GCM 加密保存，加密密钥放在 macOS 登录钥匙串。应用数据位于 ~/Library/Application Support/Safe Code，目录仅当前用户可访问。应用不会读取或迁移 Windows DPAPI 数据，也不会写入 Codex 或 CC Switch 配置。

## 本机远程控制

默认关闭。Mac 版接入 macOS 系统已有的“远程登录”，不自行启用系统服务，不修改 sshd、允许用户列表或防火墙。

使用前，需本人在系统设置 → 通用 → 共享中开启远程登录，并仅允许当前账户。允许后，本功能给当前用户 authorized_keys 添加专用受限公钥，通过已有本机 22 端口建立服务器回环反向通道，核验 Darwin 系统与真实用户 UID。此权限可以运行命令、读写或删除当前用户可访问的文件；macOS 隐私权限和需要密码的 sudo 操作仍由系统控制。

停用会撤销当前专用公钥、断开反向通道并清理服务器专用身份；不会替用户关闭原来启用的系统远程登录。应用异常中断后，再次启动会先撤销自己的残留公钥，不会自动恢复控制。没有显式测试授权时不启用本机控制。

## 第三方模型适配器

用户迁移包仅含 Windows EXE，没有可验证的 macOS 程序。此模块鸣谢“亚撒西の空白”并显示“暂不可用”，不能启动，不尝试用模拟器执行，也不发起真实模型请求。后续需取得经过审查、具有相应分发许可的 macOS 适配器，再独立验收。

## 更新与反馈

Mac 更新只匹配当前架构的 macOS DMG，不会下载 Windows EXE 或另一架构的安装包。下载后核验 GitHub SHA256；用户完成卸载后打开镜像，自行替换应用。没有 Mac 安装资产时只提供版本发布页。

沿用明确同意后的诊断与反馈流程，基础统计系统字段为 macOS。接收端新增兼容字段的源码已纳入本分支；正式对外发布 Mac 版前需确认接收端已部署该兼容更新。开发本身不隐式部署生产服务。

## 构建

```sh
python3 tools/build_macos.py --arch arm64
python3 tools/build_macos.py --arch x64
```

可选 --claude-payload 指向已核对 SHA256 的官方 Linux x64 Claude Code 2.1.311，供服务器安装时通过 SSH 上传；其余架构按现有官方校验下载流程处理。构建脚本只写本地 dist，不发布或替换已安装应用。

签名分两种模式，由 --sign-identity 决定。默认 `-`（ad-hoc）用于本机开发：不加 hardened runtime，带 `--timestamp=none`，可离线构建，产物仍需用户手动放行。传入真实 `Developer ID Application` 身份时脚本自动切到发行模式：每个 Mach-O 加 `--options runtime --timestamp`，并对 `Contents/Resources/Core/SafeCode.MacBackend` 与最外层 app 应用 `macos/SafeCode.entitlements`（.NET 后端的 JIT 与库校验、以及 osascript 的 Apple 事件都需要它，缺任一项后端会在启动后立即退出）。

公证需要额外两个参数：`--keychain-profile` 指向 `xcrun notarytool store-credentials` 保存的凭据名，`--notarize` 打开提交流程。脚本先压缩 app 提交并装订，再构建 DMG、对镜像签名、二次提交、装订并用 `spctl` 验证，最后才写 `.dmg.sha256`（签名会改动镜像字节，校验和必须在最后计算）。ad-hoc 模式拒绝 `--notarize`，避免产出“看起来已公证”的包。

## 验证记录（2026-11-15）

Apple Silicon 实机系统为 macOS 26.6.2。原生启动已实际使用本机钥匙串生成加密密钥，并检查 AppKit 菜单、无服务器预览与安全退出。AppKit / WKWebView 的桥接、独立指南确认、标准尺寸维护界面、未连接操作拒绝、只读启动及 Windows 适配器不可用检查通过。Intel x64 包另经 Rosetta 启动验收，不等同于 Intel 硬件实机验收。

Mac 原生后台通过已有 214 通道连接指定 Ubuntu 测试虚拟机，指纹与 214 已信任记录比对后，完成真实登录、巡检、目录读取；两组 Mac 本机临时目录的真实挂载、读取、改序、CloudCLI 原生项目列表登记、卸载归档、重挂身份复用和网页自动刷新资源检查均通过。测试映射已卸载，生成的项目及临时目录已清理，测试入口隧道关闭；未改动原有 Windows 用户挂载。

AES-GCM 往返及篡改拒绝、Shell 参数引用、平台和架构更新筛选通过。Mac 与 Windows 共用界面、指南、主机身份、目录边界及 CloudCLI 回归共 13 个脚本通过；诊断接收端 17 项测试通过，Windows 客户端仍以零警告、零错误编译。

本次没有开启 macOS 系统远程登录或本机控制，没有运行用户附件适配器，没有发送模型请求，没有切换 Codex / CC Switch 配置。Terminal 自动化授权、RDP 客户端实际登录，以及 Mac 本机控制真实登录需在用户主动使用或另行授权的验收中完成，不用构建或夹具成功代替。

应用使用临时本机签名，已核验签名封装与包内文件哈希；未提交 Apple 公证。构建在 ~/Library/Caches/SafeCode/Builds 下完成，避免同步目录给 .app 自动附加 Finder 元数据而破坏签名；最终 DMG 输出到 dist/macos。正式对外分发与生产反馈服务更新尚未执行。

服务器验证仅使用已指定的 Ubuntu 测试虚拟机。后续界面验收使用 --preview-sandbox <独立目录>：仅内存测试密钥、不访问真实钥匙串或用户配置，后台拒绝 SSH 连接、部署、挂载及网络请求。--smoke-test 同样使用该离线隔离模式。
