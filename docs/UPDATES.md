# 在线更新与服务端数据

客户端启动时自动查询 `Muchuaner/safe-code` 的最新正式 GitHub Release，也可在“更新与反馈”手动检查。不会把普通 commit 当成更新，不携带 GitHub token 或登录资料。检查更新与使用统计开关相互独立；GitHub 会看到正常网络请求的来源地址。

安装包名称必须为 `SafeCode-版本-Setup-x64.exe`，且下载地址属于本仓库的对应 Release。下载前需要 GitHub API 的 `sha256:` digest；缺少 digest 时仅提供正式发布页入口，不自动执行。下载只允许 GitHub 官方资产域名跳转，大小和 SHA256 均匹配才可安装；执行前再次校验。用户点击“安装更新”后，先检查是否有任务、同步卸载目录，再启动安装向导并退出客户端。不会后台强制替换运行中的文件。

当前要求保留“不上传 GitHub”的约束，因此接入机制不等于已发布版本。以后维护者上传正式 Release 的单 EXE 资产后，客户端才会提示更新。官方资产 digest 来源见 https://docs.github.com/en/rest/releases/releases#get-the-latest-release 。本地更新链路测试覆盖版本比较、地址限制、缺失摘要、下载校验、下载后文件被更改，以及不安全重定向。

反馈与统计入口由打包文件 `service-endpoints.json` 的 HTTPS 地址确定，不接受网页传入任意接收地址。首次默认勾选、确认后上报；取消勾选停止上报并跨启动保存。细节与服务器维护见 `services/feedback/README.md`。离线或上报失败不阻塞登录和部署，也不将未发送数据无限排队。
