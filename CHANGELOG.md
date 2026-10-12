## [0.4.2] - 2026-10-13

### 修复

- **WebView 缓存旧页面**：覆盖安装后 WebView 缓存残留 localhost 版页面，导致"后端未连接"。改为 `LOAD_NO_CACHE` + 启动时清缓存 + 无论后端是否就绪都强制加载页面。
- **serve.py 启动异常日志**：后端启动失败时打印 traceback，便于排查。

# Changelog

所有重要改动都记录在这个文件里。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.4.1] - 2026-10-13

### 修复

- **assets/webui.html 未同步**：安卓壳 assets 里的 webui.html 停留在 v0.3（含 `localhost:8765`），导致 APK 内 WebView 版本标识旧 + 后端未连接。已同步为 v0.4.1（强制 `127.0.0.1:8765`）。
- **清理 `__pycache__`**：安卓 python 源目录的编译缓存不再打进 APK。

### 变更

- **版本号递进规则**：以后每次更新推送递增 0.0.1 小版本号（v0.4.1 → v0.4.2 → ...），避免版本混淆。

## [0.4] - 2026-10-13

### 修复

- **app 后端未连接**：WebView 里 `localhost` 优先解析成 IPv6 `::1`，而后端只监听 IPv4 `127.0.0.1`，导致连接被拒。前端 API 地址强制改用 `127.0.0.1:8765`，绕过 IPv6 解析问题。
- **图标不显示**：补全全部 5 个密度目录缺失的 `ic_launcher_round.png`，修正 `mipmap-anydpi-v26` adaptive-icon XML 层级，重新按安全区缩放 foreground，洛宝头像图标在所有 launcher 上正常显示。

### 变更

- **本地工作区端口 8765 → 8770**：本地 Operit 工作区与手机 app 彻底隔离，app 内置后端独占 8765，两边任务历史、素材、密钥互不串扰。

## [0.3] - 2026-10-12

### 新增

- **安卓壳工程**（Chaquopy + WebView）：Python 后端完整打进 APK，手机直连 RunningHub 跑图。
- **GitHub Actions CI**：push/tag 自动构建 APK，tag 触发 Release 自动发布安装包。
- **APK 签名**：keystore 加密存 GitHub Secrets，CI 注入签名，产出可安装的 `app-release.apk`。

### 变更

- **移除 luobao 勾选注入**：AI 润色已自动注入洛宝外貌锚点，前端勾选框、后端 `LUOXUE_ANCHOR` 拼接逻辑、zimage 死代码全部清除。

### 修复

- **AI 润色 max_tokens 2000 → 256000**：支持完整长提示词输出。

## [0.2] - 2026-10-11

### 新增

- **三套分场景润色规范**：t2i（七段固定结构）/ i2i（克制短句）/ video（分镜表五列：时间｜画面｜动作｜台词｜音轨）。
- **video 时间轴强制**：分镜必须带时间段标记（0-2s / 2-4s / 4-6s），禁止无时间标记平铺。
