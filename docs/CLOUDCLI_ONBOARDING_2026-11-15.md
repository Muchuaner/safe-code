# CloudCLI 首次设置自动完成

用户自行设置 CloudCLI 网页账号与密码，随后跳过 Git Configuration 和 Connect Agents 两页，进入主界面后再按需登录 Claude Code。程序不注册网页账号、不修改密码，也不伪造代理授权状态。

安装 CloudCLI 时先为普通工作用户补齐缺失的全局 Git 姓名和邮箱。已有配置保留；缺省使用工作用户名与 `<用户名>@safe-code.invalid` 本地占位邮箱，用户可在设置中改为自己的提交身份。这里只配置 Git 提交身份，不创建或推送 Git 仓库。

在 CloudCLI 数据库内保留已有 Git 字段，为活动账号记录技术引导完成；仅为后续自行注册账号增加初始化触发器，不涉及 username、password_hash 或任何授权字段。用户自行注册的用户名、密码完整保留。初始化前的 Git 字段和引导状态保存在工作用户的 `onboarding-before-safe-code.json`，不包含密码哈希。操作由普通工作用户执行，检查主目录边界、数据库结构和事务，数据结构不兼容时停止，不修改账号。

安装阶段完成上述准备；在 Safe Code 中打开已有 CloudCLI 时也会兼容初始化，无须重新下载 CloudCLI 或重启服务。CloudCLI 原先的网页注册、登录及 Claude Code 授权逻辑均保留。

已完成 13 项自动检查，包括已有及后续自行注册账号的凭据保留、Git 配置保留、重复执行、原始状态备份和不兼容数据库拒绝。Windows 客户端编译通过，0 警告、0 错误。214 虚拟机实际 `cloud_setup` 请求成功；通过远程桌面观察到已进入 CloudCLI 主界面之上的设置页，Claude 仍显示未授权，未代替用户进行登录、更新 CLI 或发起模型请求。

214 桌面交付目录为 `SafeCode-自动初始化测试版-20261115`，其中 `SafeCode.exe` 可直接运行。沿用内置官方 Claude Code 文件的完整测试包；本地对应 ZIP 为 `.local/SafeCode-自动初始化测试版.zip`，SHA256 为 `1b14a86e0b7878548c88f0a3a3e40e8e9bb68510b457ef6f3377768931aabeea`。尚未发布 GitHub 正式更新。
