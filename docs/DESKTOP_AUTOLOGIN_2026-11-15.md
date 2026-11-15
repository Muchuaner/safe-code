# macOS「打开桌面」免密码：把 xrdp 的第二层认证也解决掉

日期：2026-11-15
适用版本：xrdp 0.9.24 / Ubuntu 24.04 / Windows App 11.4.5 (macOS)

## 现象

`prompt for credentials` 改小之后，点「打开桌面」仍然会停在要求输入密码的界面。那个界面的特征是带 `Session: Xorg` 下拉框、xrdp 图标、标题 `Login to <主机名>`——它是 **xrdp 服务端的登录窗口**，不是 macOS 客户端的凭据框。

## 为什么 `.rdp` 解决不了

RDP 认证分两层，`.rdp` 只能碰到第一层：

| 层 | 谁在问 | `.rdp` 能控制吗 |
|---|---|---|
| 客户端 | macOS 上的 Windows App | 能。`prompt for credentials:i:0` 关掉 |
| 服务端 | xrdp 用 PAM 再认证一次 | **不能**。文件里唯一的密码字段是 `password 51:b:`，那是 Windows DPAPI blob，macOS 客户端不认 |

xrdp 日志里 `Security protocol: ... selected [SSL]` 说明它只走标准 RDP 安全层，**没有 NLA/CredSSP**，所以客户端也无法在协议层把口令交给它。Windows 端之所以不用输，是因为走 RDP ActiveX 在协议层注入了凭据，macOS 这条路没有等价能力。

## 正确的开关：`autorun` + 模块级固定凭据

之前文档里建议用 `require_credentials=true`，那是错的。查 `man xrdp.ini` 原文：

```
require_credentials=[true|false]
    If set to 1, true or yes, xrdp will scan the user name provided by the
    client for the ASCII field separator character (0x1F). It will then copy
    over what is after the separator as the password supplied by the user and
    treats it as autologon.
```

也就是说它是「把密码塞进用户名字段」的 token 机制，跟关掉登录框无关。真正管用的是两条：

```
autorun=session_name
    Section name for automatic login. If set and the client supplies valid
    username and password, the user will be logged in automatically using
    the connection specified by session_name.
```

以及模块段（`[Xorg]`）的：

```
username=<username>|{base64}<...>|ask
    Specifies the username used for authenticating in the connection. If set
    to ask, user name should be provided in the login window.
password=<password>|{base64}<...>|ask
    ... If set to ask, password should be provided in the login window.
```

把 `[Xorg]` 的 `username` / `password` 从 `ask` 改成固定值，登录窗口就没有需要询问的字段，也就不会出现。

## 实现

`src/SafeCode.Core/Remote/agent.py` 的 `setup_desktop()` 在重写 `port=tcp://127.0.0.1:3389` 之后调用新增的 `desktop_autologin()`：

1. 断言 `port=` 已经是回环地址，否则拒绝写入凭据（**安全闸门**：一旦监听改成公网地址，免密等于把桌面对外开放）
2. `[Globals]` 写入 `autorun=Xorg`
3. `[Xorg]` 写入 `username=<工作用户>`、`password={base64}<密码>`
4. 文件权限 `root:xrdp 640`

第 4 步不是随手收紧。`xrdp.service` 的单元文件是 `User=xrdp`，启动时要读这份配置；设成 `0600` 会让服务直接起不来：

```
xrdp[156338]: We could not open the configuration file to read log parameters
xrdp[156338]: Error reading configuration for log based on config: /etc/xrdp/xrdp.ini
```

所以正确权限是「root 拥有、xrdp 组可读」，而不是 600，也不是 644。

`src/SafeCode.MacBackend/MacClients.cs` 相应把生成的 `.rdp` 改成：

- `prompt for credentials:i:0` —— 客户端不再问，服务端已有凭据
- 去掉 `dynamic resolution:i:1` —— 见下节

## 会话复用：为什么必须去掉 dynamic resolution

sesman 用 **宽 / 高 / bpp** 匹配已有会话，尺寸不一致就当作新会话，而旧会话在 `KillDisconnected=false` 下不会被回收。实测：

```
[20261114-22:07:39] Starting session: display :10.0, width 1280, height 800
[20261115-20:48:31] Starting session: display :11.0, width 1512, height 949
```

两次不同的几何尺寸 → 两个常驻会话。实测杀掉其中一个，`free` 显示可用内存从 822 MB 回到 1387 MB——**单个 XFCE 会话占约 560 MB**。在 2 GB 的机器上，这种泄漏几次就会把内存吃光。

去掉 `dynamic resolution` 后，全屏模式本来就用屏幕尺寸，几何尺寸因此稳定，重连变成复用：

```
[20261115-20:52:31] ++ reconnected session: username workspace, display :11.0
```

## 实测证据（2026-11-15，主机 61.111.253.45 / xrdp 0.9.24）

配置生效后点「打开桌面」，服务端日志：

```
[20261115-20:48:31] ++ created session (access granted): username workspace
[20261115-20:48:31] login successful for user workspace on display 11
[20261115-20:48:31] Session started successfully for user workspace on display 11
```

**没有任何 AUTHFAIL、没有等待输入，从连上到 login successful 是同一秒。** X 会话侧确认 XFCE 已起：

```
157343 xfce4-session
157437 xfwm4
xwininfo -root -tree → 33 children（面板、桌面、fcitx 都在）
```

## 安全取舍

凭据以 `{base64}`（编码不是加密）写进 `/etc/xrdp/xrdp.ini`。几点说明：

- 这份密码本来就已经以明文存在 `/var/lib/safe-code/account.json`（`0600`），只是复制到第二处，没有引入新类别的暴露
- 文件权限 `root:xrdp 640`，只有 root 与 xrdp 用户可读
- xrdp 只监听 `127.0.0.1:3389`，能连上的前提是已经通过 SSH 认证并建立了隧道；也就是说**认证边界从 xrdp 前移到了 SSH**
- 若将来把监听改成非回环地址，`desktop_autologin()` 的闸门会拒绝写入；已经写入过的需要手动回滚

## 已部署的服务器怎么补

重跑一次「安装虚拟桌面」会连带重装整套软件包，代价偏大。只想补这一步时，用独立的 `tools/xrdp_autologin.py`（同样的逻辑，幂等，可直接重复执行）：

```bash
sudo python3 tools/xrdp_autologin.py
# OK autorun=Xorg
# OK [Xorg] username=workspace
# OK [Xorg] password={base64}<27 字符>
# OK 权限 root:xrdp 640，xrdp 用户可读
# xrdp: active
```

它读 `/var/lib/safe-code/account.json` 取凭据，所以不需要在命令行里传密码。

## 回滚

```bash
sudo cp /var/lib/safe-code/backup-xrdp.ini.autologin /etc/xrdp/xrdp.ini
sudo systemctl restart xrdp
```

`/var/lib/safe-code/backup-xrdp.ini` 是安装虚拟桌面时留下的更早备份（首次改动前）。

## 仍未覆盖

- 换了显示器或改了分辨率预设时，几何尺寸变化会产生新会话，旧会话留在内存里。目前靠 `reconnected session` 覆盖正常场景，没有做自动清理
- 客户端侧如果 Windows App 后来引入 NLA 支持，服务端配置可以简化，但现在这套在 0.9.24 上工作正常
