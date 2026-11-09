# Safe Code

[下载 Beta 测试版](https://github.com/Muchuaner/safe-code/releases/tag/Beta-0.4.3.2) · Windows 10 / 11 x64

面向海外中文用户的开源 VPS 开发环境配置工具。当前 Beta 测试版已包含 Windows 原生宿主、真实 SSH 部署、维护操作与进阶选项；当前发布为 **Beta-0.4.3.2 测试版**。软件面向海外中文用户，中国大陆禁止使用。

## 运行 Windows 客户端

运行单个 `SafeCode-Beta-0.4.3.2-Setup-x64.exe` 安装文件。内含 .NET 和 WebView2 离线依赖；无需 Python、Node.js、WSL 或 .NET SDK。详见 [Windows 使用说明](docs/WINDOWS.md)。

输入 IP、用户名、密码，核对首次 SSH 指纹。**仅 Ubuntu 可以安装；非 Ubuntu 在任何部署写入前明确拒绝。** 安装管理员需为 root 或免密 sudo。支持目标为 Ubuntu 22.04 / 24.04 x86_64 的 systemd VPS，推荐 4GB 内存；实际环境验证为 Ubuntu 24.04 与 Windows 10 19045 x64。

## 功能

- 七步部署：普通工作用户及机器名称、本土化、XFCE / xrdp、Chrome、中文拼音、Claude Code / Git / tmux、工作用户 SSH 密钥。单项或勾选批量执行，已完成项不能重复选择，输出真实日志。
- 独立远端部署任务，断线不终止包安装；重连恢复监控，完成状态来自实际软件与服务检查。
- 维护卡片：工作账户及复制、Windows 原生远程桌面组件、真实 OpenSSH 终端。关闭应用释放本次连接资源。
- 多目录挂载：本机目录经会话认证的回环文件服务和反向 SSH 通道挂载到 VPS，支持保存多组映射，退出前同步卸载。
- CloudCLI：安装固定版本并以普通用户、回环地址运行，SSH 隧道内一键打开网页，账号授权由本人完成。
- 出入口 IP 锁定 / 更换：只接受已分配地址，验证新连接、实际出口；独立定时回退、失败关闭与开机恢复。只管理本软件的策略，不清空系统规则。
- 按出口 IP 综合定位推荐时区与语言，提供手动选择。Net.Coffee 多源定位优先于注册地简表；平票时要求确认。

## 构建与项目结构

开发需要 .NET 10 SDK。Windows 宿主使用 WPF + WebView2，沿用已确认的极简界面；SSH、权限检查、安装、隧道、文件桥与系统客户端调用在 C# / Ubuntu 模块中完成。

```sh
dotnet build src/SafeCode.Desktop/SafeCode.Desktop.csproj
dotnet publish src/SafeCode.Desktop -c Release -r win-x64 --self-contained true -o dist/win-x64
```

带许可证、完整文件清单与哈希的便携包可用构建辅助程序生成（开发机需 Python 3）：

```sh
python3 tools/build_portable.py --dotnet dotnet
```

- `src/SafeCode.Core`：SSH 核心、独立作业、目录共享、IP 策略与地域解析。
- `src/SafeCode.Core/Remote`：Ubuntu 部署与网络策略；检查 OS 后才允许写入。
- `src/SafeCode.Desktop`：Windows 宿主、最小化 / 最大化 / 关闭、系统客户端与受限本机桥接。
- `native.js`：真实桌面桥接。未成功加载时桌面版禁止退回模拟部署。
- `src/SafeCode.Verify`、`tools/test_*.py`：集成与边界测试工具；测试凭据须由本机私密配置提供，不包含在源码中。

## 浏览器概念预览

原始浏览器交互概念稿仍可使用：

```sh
python3 -m http.server 5173 --bind 127.0.0.1
```

打开 http://127.0.0.1:5173/ 。**普通浏览器模式仍模拟 SSH / 安装，不可作为部署结果；Windows 客户端才调用真实后端。** 预览省略了模拟标签以便观察布局。请勿在概念稿输入真实密码。

## 安全与数据边界

成功登录的信息通过 Windows DPAPI 按当前用户整体加密保存，不打印密码。工作用户与管理员分离，生成独立随机密码；本机密钥限制访问权限。巡检不会以 root 执行用户可写程序，用户配置写入以普通用户进行，拒绝越界符号链接。目录桥拒绝未认证请求、路径穿越和符号链接逃逸；RDP / CloudCLI 默认只监听 VPS 回环。

IP 评分仅向 Net.Coffee 发送出口 IP；出口探测访问 ipify。不自动上传账号、密码或目录内容。基础使用统计默认勾选，在声明页确认后上报，可随时关闭；主动反馈仅发送用户填写的文字和软件版本。第三方工具自身的账号授权和许可由原厂提供。详细限制见 [Windows 使用说明](docs/WINDOWS.md) 与 [测试记录](docs/VERIFICATION.md)。

## 项目入口与许可证

[GitHub](https://github.com/Muchuaner/safe-code) · [QQ 群](https://qm.qq.com/q/cyUWAwYKQg) · [VPS 推荐](https://www.rainyun.com/Agentom_)

自有源码采用 [MIT](LICENSE)。第三方依赖及单独安装组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

Windows 安装包在运行便携包构建后，用 Inno Setup 7 编译 `tools/windows-installer.iss`。默认读取 `dist/SafeCode-Beta-0.4.3.2-win-x64` 和 `.local/installer/MicrosoftEdgeWebView2RuntimeInstallerX64.exe`，输出至 `dist`；也可通过 `AppSource`、`RuntimeSource`、`SetupOutput` 预处理参数指定绝对路径。离线 Runtime 从微软官方 WebView2 Standalone x64 下载，并在构建机验证 Microsoft Corporation 的有效数字签名。编译与打包均不触发 GitHub 发布。

更新与反馈入口提供 GitHub 正式 Release 检测、SHA256 校验下载与用户确认安装。基础使用统计和主动反馈接入独立 HTTPS 服务，详见 [在线更新](docs/UPDATES.md) 与 [服务端维护](services/feedback/README.md)。
