# 远程桌面链路完善（2026-11-15）

范围：macOS 端「远程桌面」功能，即 `desktop` 操作从建立隧道到交给 RDP 客户端这一段。不含服务器端 xrdp 安装与 XFCE 部署。

## 结论

原实现能用，但每用一次就退化一次：本地端口随机、隧道不复用、`.rdp` 文件按端口命名、隧道死了客户端只报通用错误。四处叠加的结果是——**每次打开远程桌面在 Windows App 里都变成一台新机器，密码要重新输一遍，重连几次之后列表里全是 `127.0.0.1:54957` 这类垃圾条目**。这不是美观问题，是每次使用都要多花十几秒的固定成本。

## 问题与改动

| 问题 | 表现 | 改法 |
|:---|:---|:---|
| 本地端口随机 | `ForwardedPortLocal(..., 0, ...)` 每次绑定随机高位端口 | `ForwardAsync` 增加 `preferredPort` 参数，桌面通道优先绑 `127.0.0.1:13389`，被占用才回落随机 |
| 隧道不复用 | 每点一次「远程桌面」就多一个监听端口，从不回收（实测同时存在 54229 / 54957） | `forwards` 字典按目标端口缓存，`IsStarted` 时直接复用已绑定端口 |
| `.rdp` 按端口命名 | `Safe Code-<port>.rdp`，一次会话一个文件 | 固定为 `Desktops/Safe Code.rdp` 覆盖写，并清理历史 `Safe Code-*.rdp` |
| 隧道断开无提示 | 客户端弹「无法连接到远程计算机」 | 打开前对 `127.0.0.1:<port>` 做一次 TCP 探测（1.5 秒上限），失败则明确告知隧道已断、需重新连接服务器 |
| 只探到本地监听 | 端口能连上不代表服务器上的 xrdp 活着 | `DesktopAsync` 建好隧道后发一次 X.224 连接请求，5 秒内无应答则报「虚拟桌面没有应答」，与「隧道断了」区分开 |
| xrdp 自签证书告警 | 每次连接都弹「无法验证服务器身份」 | `.rdp` 用 `authentication level:i:0`；连接只走回环，证书必然自签，该提示只会训练用户盲点“继续” |

## `.rdp` 字段

| 字段 | 值 | 理由 |
|:---|:---|:---|
| `full address:s:` | `127.0.0.1:13389`（或回落端口） | 只用于 SSH 隧道入口 |
| `username:s:` | 工作用户名，来自 `account_info` | 免去在客户端里再敲用户名 |
| `prompt for credentials:i:1` | 开 | 密码仍由客户端输入并自行保存，不落盘到 `.rdp` |
| `desktopwidth` / `desktopheight` | 取 `preferences.ReadResolution()` | 界面上选的分辨率此前只影响 Windows 端的原生窗体 |
| `authentication level:i:0` | 连接且不告警 | 见上 |
| `dynamic resolution:i:1` | 开 | 拖动窗口时远端分辨率跟随 |
| `redirectclipboard:i:1` | 开 | 本机与远端共享剪贴板 |
| `audiomode:i:2` | 不播放 | xrdp 不支持音频重定向，保留默认值会让协商多一轮无效尝试 |
| `redirectprinters:i:0` | 关 | 本机没有可转发的打印机服务 |

端口固定带来的连带收益：Windows App 以地址区分连接，`127.0.0.1:13389` 现在每次都是同一台“机器”，客户端那一层的凭据可以长期保存，连接列表里也不会再堆积随机端口条目。

**第二层认证已单独解决。** 客户端凭据只覆盖第一层；xrdp 收到连接后会用 PAM 再认证一次，弹出带 `Session` 下拉框的登录窗口——那是服务端界面，`.rdp` 带不过去（文件里的密码字段是 Windows DPAPI blob，macOS 客户端不认）。现在由「安装虚拟桌面」在服务端写入 `autorun` 与 `[Xorg]` 固定凭据，登录窗口不再出现。机制、实测证据与安全边界见 `docs/DESKTOP_AUTOLOGIN_2026-11-15.md`。

## 首次使用

点「远程桌面」。没有可用客户端时应用自己下载并安装官方 Windows App（进度面板显示，约 90 MB，无需管理员密码），装完直接打开当前 `.rdp`；已有客户端则直接复用。**客户端与服务端都不再要求输入密码，点一下直接进桌面。**

```bash
# 服务端凭据由「安装虚拟桌面」自动写入；手工回滚：
sudo cp /var/lib/safe-code/backup-xrdp.ini.autologin /etc/xrdp/xrdp.ini
sudo systemctl restart xrdp
```


## 验证状态

- **已编译**：`dotnet build src/SafeCode.MacBackend -c Release` 通过，0 warning / 0 error；`swiftc -typecheck` 与完整链接均通过（需 `-sdk /Library/Developer/CommandLineTools/SDKs/MacOSX15.sdk`，SDK 27 与 6.3.3 编译器不匹配）。
- **未实机连接**：端口复用与回落路径需要真实服务器验证；重点看 `~/Library/Logs/Safe Code/backend.log` 与 `lsof -nP -iTCP:13389`。
- **2026-11-15 实机补充（macOS 26.6.2 + Windows App 11.4.5 + xrdp 0.9.24 + Ubuntu 24.04）**：固定端口生效（`lsof` 只有一条 `127.0.0.1:13389`），旧的三份 `Safe Code-*.rdp` 被自动清理，只留 `Safe Code.rdp`。第二轮进一步确认：写入 `autorun` 与 `[Xorg]` 固定凭据后，连接在 1 秒内 `login successful`，**两端都不再出现密码提示**；同时实测到 `dynamic resolution` 会让会话因几何尺寸变化而新开，单个会话约 560 MB，故已移除该选项（详见 `docs/DESKTOP_AUTOLOGIN_2026-11-15.md`）。
- **X.224 探活的误报边界**：探测成功不代表登录一定能成（xrdp 认证、会话数上限仍可能失败）；探测失败一律视为不可用，5 秒超时对回环隧道是宽裕值，正常情况在几十毫秒内返回。
- **端口固定的边界**：若 13389 被其他程序长期占用，会静默回落到随机端口，此时“记住密码”的收益消失但不影响连接；同时开两个服务器会话共用 13389 时，第二个会回落。
- **`authentication level:i:0` 的取舍**：这等于对本机回环连接关闭证书校验。因为流量不出回环、且目标是本机建立的 SSH 隧道，判定为可接受；若将来隧道改为非回环监听，必须改回告警。
- **隧道复用只覆盖同一次会话**：后端进程重启后 `forwards` 为空，会重新抢占 13389；端口一致，客户端侧无感。
