# S2 Dashboard

S2 中证红利策略仪表盘，提供 Windows 和 macOS 发布包。

## 发布与数据隔离

公开仓库只包含应用源码、公开配置和公共历史数据，不包含任何本地账户、真实资产、Pending、日志、备份或原始行情数据库。安装包首次启动时创建空的本地 OWNER 账户：

- Windows：`%LOCALAPPDATA%\S2Dashboard`
- macOS：`~/Library/Application Support/S2Dashboard`

应用更新只替换程序文件，不删除或覆盖上述用户数据目录。

## 本地开发

需要 Python 3.12+。安装依赖后，Windows 和 macOS 都可以从 `app` 目录启动 FastAPI 后端。

## 下载

每个 GitHub Release 同时提供 Windows 安装器和 macOS `.dmg`/`.zip`，并附带 `SHA256SUMS.txt`。构建在对应原生操作系统上完成，避免跨平台打包造成运行时问题。
