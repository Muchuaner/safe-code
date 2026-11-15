# 登录教程、账户修改确认与服务器改密恢复

## 界面行为

登录页“连接服务器”下方增加同色的半宽“高级连接”，右侧为“这些信息在哪？”。点击教程先展开窗口，再展示 VPS 控制台登录信息示意图；关闭后恢复登录窗口宽度。教程说明服务器登录密码、服务商网站账户密码及工作用户密码的区别，提醒控制台未必同步显示在 SSH 中手动修改后的密码。

示意图由内置 imagegen 编辑用户提供的第一张截图，白底、细分隔线、绿色编号，突出密码“查看”，原 IP 替换为示例 `203.0.113.86`。资产为 `assets/server-login-tutorial.png`，已加入 Windows 项目资源与 macOS 打包脚本。

两个“自定义”使用部署按钮的轮廓，已完成账户步骤也保留轮廓。账户首次保存只存草稿；第二次起，或服务器已有完成的工作账户时，保存前出现确认：“我不改了”取消本次保存，“我知道自己在做什么，更改并应用”保存并直接执行账户更新。关闭确认窗口等同取消。重复提交会被锁定。确认历史按端点及服务器指纹存于本机，成功应用后清除草稿仍保留历史；失败时保留草稿供重试。

## root 密码变更

用户在本轮进一步确认，粉丝在服务器侧修改了 root 密码。它不会自动更新 Safe Code 进程中已保存的密码；已认证会话可能暂时存活，但新建挂载 SSH、SFTP 和恢复控制连接继续使用旧密码，会认证失败。这是对前一次反馈调查中“原因未确认”的补充，依据为用户后续提供的信息，不是匿名诊断额外采集到了密码。

主界面服务器卡片新增“更新登录密码”，断线时仍可打开，只允许为同一端点、同一登录用户输入新密码。客户端核对原服务器公钥，完成新 SSH 认证、Ubuntu/权限检查、SFTP 与部署模块准备后，才替换控制连接和凭据。错误密码或准备失败保留原会话和文件桥；不更改远端账户密码、不自动信任变化的主机密钥。

恢复保持原文件桥、客户端标识、远端端口、目录映射和待上传缓存，不通过强制卸载或丢弃缓存解决退出问题。挂载或本机控制守护遇到认证失败会暂停自动认证重试，提示更新密码；普通网络中断仍重试。新凭据验证后更新本机加密登录记忆，并恢复挂载。原主控制连接上的桌面/网页转发需要重新打开，界面有提示。恢复后的挂载仍可能因服务器网络、端口占用等原因继续重试，界面显示实际状态，不承诺验证密码成功即所有挂载必定恢复。

## 验证与交付范围

- Windows 客户端和 macOS 后端编译通过，0 警告、0 错误。
- `tools/test_login_help_ui.py`：窄登录窗口布局、教程图片加载、端口弹窗、首次不提醒、第二次取消不保存、确认直接应用、已完成步骤的自定义轮廓、密码错误后重试和认证提示检查通过。
- `tools/test_credential_recovery.py` 使用本地真实 SSH/SFTP 协议夹具：原密码失效、新密码验证、错误密码不覆盖凭据、认证成功但 SFTP 准备失败保留旧会话、原文件桥/端口/挂载标识保持、安全卸载、变化的主机密钥拒绝，以及账户确认历史保留检查通过。测试不修改系统 root 密码，不连接粉丝 VPS；挂载远端为协议夹具，并非本轮真实 Linux FUSE 内容恢复验证。
- 原空目录创建及挂载状态界面回归通过。macOS 隔离后端对新增密码恢复 RPC 同样禁止网络/修改操作。
- 截图位于 `.local/evidence/login-recovery/`。本轮完成开发和本地验证，未发布正式更新，未替换 214 正在运行的客户端。

## 生图提示词

Use case: text-localization. Asset type: Safe Code 登录教程弹窗内的中文示意图。Edit target: provided VPS hosting panel screenshot. Preserve the recognizable simple flat white table, thin gray dividers, labels 远程连接 / 公网 IP 地址 / 远程用户名 / 远程密码 / 更多, copy and eye icons. Replace original IP everywhere with documentation example 203.0.113.86, username root, password remains masked with asterisks, never show actual secrets. Polish typography and spacing, crisp Chinese text. Add small muted dark green numbered circles 1 next to 公网 IP 地址, 2 next to 远程用户名, 3 next to 远程密码, and a restrained green rounded outline around 查看 to point users to password reveal. Add one compact footer below the table reading “在 VPS 服务商控制台找到「远程连接」，查看并复制登录信息。” and smaller “示例信息，请以你的服务器为准”。No extra headings, no gradients, no arrows, no decorations, no 3D. Horizontal wide image around 1200 by 600, clean white background and modest margins, matching a professional minimal green desktop app. Output only the tutorial image.
