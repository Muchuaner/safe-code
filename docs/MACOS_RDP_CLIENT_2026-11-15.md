# macOS 一键打开远程桌面

日期：2026-11-15

## 问题

Windows 端「远程桌面」卡片点一下直接进 mstsc。macOS 端同样的点击停在「需要先安装远程桌面客户端」的弹窗，用户必须自己找到 Microsoft Windows App、下载、安装，再回来重新点一次。这一轮把这段补上。

根因是一条平台事实：**macOS 不内置 RDP 客户端**。Windows 的 `mstsc.exe` 属于系统组件，macOS 没有等价物，系统自带的「屏幕共享」只讲 VNC。因此在 Mac 上「一键进桌面」必须先解决「客户端从哪来」。

## 方案

`macos/RDPClient.swift`（新增）负责客户端解析与准备，`macos/SafeCode.swift` 只保留「探测隧道 → 交给 RDPClient」的编排。解析顺序按代价从低到高：

| 顺序 | 来源 | 选择理由 |
|---|---|---|
| 1 | 应用内 `Contents/Resources/RDP/sdl-freerdp` | 完全离线，版本随应用固定，不需要 Launch Services 注册 |
| 2 | 已安装的 Microsoft Windows App | 官方客户端，Retina 与剪贴板行为正确，是 Windows 端体验的对齐目标 |
| 3 | Homebrew 的 `sdl-freerdp` | 避免为了用一个客户端再下 90 MB |
| 4 | 以上都没有 → 自动下载并安装官方客户端 | 兜底，用户不需要做任何选择 |

只认 `sdl-freerdp` 而不认 `xfreerdp`：后者在 macOS 上依赖 XQuartz，把它当作可用客户端会启动一个永远不出现的窗口。

Windows App 用 `NSWorkspace.open(_:withApplicationAt:configuration:)` 显式指定应用路径启动，而不是靠 `.rdp` 文件关联。刚装好的应用还没被 Launch Services 登记过 `.rdp` 关联，用 `NSWorkspace.open(url)` 会失败或弹「选择打开方式」。

## 自动安装

不做 `installer -pkg`：官方 pkg 走 Installer.app 会要求管理员密码，弹一串许可协议，还会写系统 receipts。改成用户级安装：

1. 下载 `https://res.public.onecdn.static.microsoft/.../Windows_App_11.4.5_installer.pkg`
2. 校验 SHA-256（固定值写在代码里，与 Homebrew cask 清单同源）
3. `/usr/sbin/pkgutil --expand-full` 展开到临时目录
4. 在展开树里找 `CFBundleIdentifier == com.microsoft.rdc.macos` 的 app
5. `codesign -dv --verbose=4` 校验 `TeamIdentifier=UBF8T346G9`
6. `ditto` 复制到 `~/Applications`
7. `xattr -dr com.apple.quarantine` 去掉隔离标记
8. 用显式应用路径打开 `.rdp`

校验和与团队标识两道关，是因为单靠 HTTPS 无法覆盖镜像被替换的情况；隔离标记之所以能去掉，前提正是第 2 步和第 5 步已经判定来源可信。

`pkgutil` 位于 `/usr/sbin` 而不是 `/usr/bin`，这一条在实测中直接踩到。

## 实测中暴露的两个缺陷

**1. Launch Services 会返回已经移动或删除的应用路径。** 原实现用
`NSWorkspace.urlForApplication(withBundleIdentifier:)` 找到客户端就当作可用。自检时把 app 移到 `/tmp`，这个接口依然返回 `/private/tmp/WA-backup.app` 并把它当成可启动的客户端——用户删掉 Windows App 之后，每次点「打开桌面」都会静默失败。修复：拿到路径后再做一次 `fileExists` 校验。

**2. 安装包里不止一个 `.app`。** 第一次跑通安装流程，装上去的是 `Microsoft AutoUpdate.app`——枚举顺序靠运气，而它同样由 Microsoft 签名，所以签名校验放行了。修复：先按 `Windows App` 前缀匹配，再退化为按 bundle identifier 匹配，并在安装前断言 `CFBundleIdentifier == com.microsoft.rdc.macos`。

第 2 个缺陷的代价很具体：`~/Applications` 里原有的 `Microsoft AutoUpdate.app` 被同版本 payload 覆盖了一次。内容一致，无功能影响，但说明「只看签名不看身份」是不够的。

## 面板

下载是应用里唯一的分钟级等待。用非模态面板（`ProvisionPanel`）而不是模态 alert：模态会冻结 WebView，用户既看不到进度也无法切走。面板有确定进度条（下载）与不确定进度条（解包安装）两种状态。

## 验证记录

本机 macOS 26.6.2 / arm64，真实执行，不是静态检查：

| 项目 | 结果 |
|---|---|
| 客户端发现 | 移走已装客户端后 `LOCATE none -> provisioning` |
| 完整准备流程 | `PROVISION windowsApp: ~/Applications/Windows App.app`，**9 秒** |
| 安装结果 | 版本 11.4.5，`com.microsoft.rdc.macos`，`Developer ID Application: Microsoft Corporation (UBF8T346G9)` |
| 隔离标记 | 已清除（仅余 `com.apple.provenance`，属正常） |
| 一键打开 | `LAUNCH OK`，`Windows App` 进程 95328 起来 |
| 应用构建 | 217 文件，`valid on disk` / `satisfies its Designated Requirement` |
| 冒烟测试 | `success:true`，6 项断言全过 |
| 后端口编译 | 0 warning / 0 error |

`tests` 目录下的既有 Playwright UI 用例未运行（PyPI 拉取失败），本轮改动不涉及 `index.html` 与页面脚本，只动壳层与 `.rdp` 内容。

## 仍未验证

- **真实服务器连接**：本机到 `210.121.44.163:22` 不可达（443 通、22 超时；经代理也读不到 SSH banner，对照组 `github.com:22` 同样如此，判定为出口节点屏蔽 22 端口），因此「装好客户端 → 隧道 → xrdp 出画面」这一段无法端到端验证。
- **Intel 架构**：只在 arm64 上跑过。`pkgutil` / `ditto` / `codesign` 都是通用系统工具，风险低，但没有实机。
- **FreeRDP 分支**：`sdl-freerdp` 的启动参数（`/v:`、`/cert:ignore`、`+clipboard`、`/dynamic-resolution`、`/size:`）按 FreeRDP 3.x 语法写，未在装了 FreeRDP 的机器上实跑。
- **Windows App 版本漂移**：下载地址与校验和写死在源码里。微软发布 11.5 之后，旧链接仍可用；一旦旧链接下线，需要同步更新这两个常量。这里刻意不做「自动查询最新版本」——那会把一次可审计的固定下载变成不可预测的远程代码来源。
