# macOS 发行链路与窗口体验优化（2026-11-15）

范围：Developer ID 签名/公证闭环、原生窗口与标题栏、后端可诊断性、RDP 客户端探测。不涉及 Windows 端、服务器端与界面业务逻辑。

## 结论

原构建脚本只能产出 ad-hoc 开发包，即使补上开发者证书也无法通过 Apple 公证：三处硬性条件全部缺失——`--options runtime`、安全时间戳（脚本固定 `--timestamp=none`）、entitlements。同时 .NET 后端在 hardened runtime 下会立刻退出，因此“先加 runtime 再签名”不是可选优化，而是必须先配 entitlements 才能上 runtime。本次按此顺序补齐。

## 改动

| 位置 | 改动 | 原因 |
|:---|:---|:---|
| `macos/SafeCode.entitlements`（新增） | `allow-jit`、`allow-unsigned-executable-memory`、`disable-library-validation`、`automation.apple-events` | 自包含 .NET 后端需要可执行内存映射与旁加载原生库；SSH 终端调用 osascript 需要 Apple 事件授权 |
| `tools/build_macos.py` | `--sign-identity` 非 `-` 时自动加 `--options runtime --timestamp` | 公证的硬性要求；ad-hoc 模式保持 `--timestamp=none` 可离线 |
| 同上 | 新增 `--entitlements`（默认指向 `macos/SafeCode.entitlements`） | 发行模式缺 entitlements 直接拒绝构建 |
| 同上 | 新增 `--notarize`、`--keychain-profile` | 压缩提交 → 装订 app → 构建 DMG → 签名镜像 → 再提交 → 装订 → `spctl` 验证 |
| 同上 | 嵌套签名改为深层优先，entitlements 只加在 `SafeCode.MacBackend` | 由内向外才是正确签名顺序；普通 dylib 不应携带 entitlements |
| 同上 | `.dmg.sha256` 移到所有签名动作之后 | 对镜像 `codesign` 会改写字节，先算校验和必然失效 |
| 同上 | Info.plist 增加 `CFBundleInfoDictionaryVersion`、`CFBundleDevelopmentRegion`、`LSApplicationCategoryType` | 缺失项导致“关于”面板与系统归类不完整 |
| `macos/SafeCode.swift` | 窗口加 `.fullSizeContentView`，标题栏透明且隐藏标题文字 | 界面自己画了标题栏，原生标题栏叠在上面会出现两条栏和两处 “Safe Code” |
| 同上 | `setFrameAutosaveName`，`window.resize` 不再强制 `center()` | 窗口位置与尺寸此前每次重开都丢失；切紧凑/宽屏时窗口会跳回屏幕中央 |
| 同上 | 实现 `applicationSupportsSecureRestorableState` | macOS 14+ 未实现会在控制台告警并禁用状态恢复 |
| 同上 | 后端 stderr 写入 `~/Library/Logs/Safe Code/backend.log`（超 2 MB 轮转，预览/冒烟模式仍丢弃） | 原本指向 `nullDevice`，现场故障无任何线索 |
| 同上 | RDP 探测改为“Launch Services + bundle id + 安装路径”三级回退，缺失时给下载入口 | 客户端刚安装、尚未启动过时 Launch Services 不认识 `.rdp`，会被误判为未安装 |
| `native.js` | macOS 分支给 `.titlebar` 加 `padding-left:78px`、`min-height:32px` | 全高标题栏后红绿灯压在应用名上；原 `padding-right:18px` 在隐藏自绘控件后已无意义 |

## 未做（需要单独决定）

- **深色模式**：`styles.css` 无任何 `prefers-color-scheme` 分支，系统深色下仍渲染浅色界面。补齐需要按现有配色重新取一套暗色变量，不是加几行媒体查询能收敛的，未擅自改动。
- **二次崩溃后的恢复**：`restarted` 标志只允许重启后端一次，第二次崩溃即彻底断开。改为退避重试会影响现有冒烟断言，留给单独变更。
- **Universal 二进制**：仍是 arm64/x64 两份产物。合并会把自包含 .NET 运行时叠成单包双份，收益与体积需先权衡。
- **窗口拖拽**：`window.drag` 仍依赖 `NSApp.currentEvent`，在非鼠标按下链路下可能取到 nil；改为原生拖拽热区属于结构性改动。

## 验证状态

- `swiftc -typecheck` 与完整编译链接均通过；`node --check native.js` 通过；`python3 -m py_compile tools/build_macos.py` 通过。
- 本机 Command Line Tools 的默认 SDK（27.0）由 Swift 6.4 构建，与随附的 swiftc 6.3.3 不匹配；用 `-sdk /Library/Developer/CommandLineTools/SDKs/MacOSX15.sdk` 指定旧 SDK 即可正常编译。构建脚本为此增加了可选的 `--macos-sdk`，不传时行为不变。
- `dotnet build` 四个项目（Core、MacBackend、Verify、Windows Desktop）全部 0 warning / 0 error。
- 未持有 Developer ID 证书，hardened runtime 与公证路径**未实机验证**；启用 runtime 后必须确认后端仍能启动（若立即退出，优先检查 `allow-jit` 是否随包生效：`codesign -d --entitlements :- "<app>/Contents/Resources/Core/SafeCode.MacBackend"`）。
- 窗口改动未做桌面截图复核：本机无屏幕录制授权，仅有安装版 `--smoke-test` 的 WebView 快照（wide 模式布局正常，自绘控件在 macOS 已隐藏）。
