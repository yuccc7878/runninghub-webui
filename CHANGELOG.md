## [0.5.5] - 2026-10-22
### 变更
- **Krea2 合并为单入口**：`krea2_t2i` / `krea2_i2i` 两项合并为 `krea2` 一项，与 `zimage_i2i` 结构对齐——**选图 = 图生图，不选图 = 文生图**。
  - `rh_krea2_app.run()` 本来就有自动分流能力，拆两项纯属多余
  - 参数统一为 8 个：`ref_image` / `resolution` / `steps` / `denoise` / `style` / `realism` / `body` / `seed`
  - LoRA 三位默认强度改按 App 动态取（`LORA_DEFAULT_STRENGTH`），不再需要两套注册项来承载不同默认值
- **前端卡片标签改为按参数推导**，不再硬编码脚本 key：
  - `webui.html` 的 `modeTag`：有 `ref` 参数 → 图文双模 / `mode==='t2i'` → 文生图 / 否则图生图
  - `currentKind()`（AI 润色风格选择）：同样按 `mode` + `ref` 参数推导
  - 此前的 `k==='zimage_i2i'` 与 `k==='krea2_t2i'` 硬编码已移除，新增脚本无需再改前端

### 不兼容
- **旧 krea2 历史任务已清除**：`webui_state.json` 中 `script` 为 `krea2_t2i` / `krea2_i2i` 的记录全部删除，不再迁移。此前仅在少数测试者间分发，无需保留。
- 若曾收藏或脚本化调用 `/api/submit` 并传 `script: "krea2_i2i"`，请改为 `script: "krea2"`。

### 验证
- 合并后实跑一张 T2I（无参考图）：taskid `2113041109014110209`，832×1216，约 56 秒，正常出图。

## [0.5.4] - 2026-10-22
### 头条
- **两个生图工作流换新，又快又省**：Krea2 与 Zimage 全部切到已发布 App 通道（`ai-app/run`），不再走 `/proxy` 自己拼 prompt。
  - `krea2_t2i` / `krea2_i2i` → **krea2快速省币**（`2113009215693500417`）/ **krea2图生图极速省币**（`2113025166677749762`），实测单张约 30-60 秒、约 5-12 币
  - 节点结构由 App 锁定，平台改画布也不会再把本地脚本跑挂
  - 三个 LoRA 槽位（28 画风 / 29 写实 / 30 身材）全部支持运行时换名，不必重发布

### 新增
- **`scripts/rh_krea2_app.py`**：Krea2 双 App 调用模块（`APP_T2I` / `APP_I2I`），对齐 `rh_zimage_app.py` 的双模结构，`run()` 按有无参考图自动分流。
- **LoRA 默认强度按 App 分开**（`LORA_DEFAULT_STRENGTH`）：两个 App 是独立发布的，同一槽位默认值并不相同——

  | 槽位 | T2I | I2I |
  |---|---|---|
  | 28 画风 | 1.0 | 0.8 |
  | 29 写实 | 0.5 | 0.6 |
  | 30 身材 | 0.85 | 1.0 |

  此前误用同一套默认值，「留空沿用发布默认」时 I2I 的画风/身材强度会悄悄偏移。
- **`sync_defaults()` 自检**：调平台 `apiCallDemo` 拉权威默认值，与本模块写死的值逐项比对，不一致直接报差异。`python3 scripts/rh_krea2_app.py` 即触发。
- **任务失败后由用户决定是否重提**：新增 `resubmit_pending` 状态与前端确认弹窗（`rsBox`）。
  - 远端明确失败 / 任务号不存在 → 标记 `error` + 挂起询问，**不再自动重提**
  - 「确认重提」走 `POST /api/task/resubmit`，「取消」走 `POST /api/task/resubmit/dismiss`
  - 网络异常（远端状态未知）保持原状态不动，绝不据此重提——避免网络抖动导致同一任务重复扣币
- **`rh_krea2_app.check_app_task()`**：App 通道任务状态查询，替代 `/proxy/history`。

### 修复
- **断点续跑全盘失效（严重）**：`_recover_stale_tasks` / `_inspect_task` 查远端前没有注入 API Key，进程重启后模块内 `API_KEY` 为空串，查询恒返回 `301 must not be blank`，被归类为 `api_error` 后走「保持原状态」，任务永远卡在 `submitting`，提交锁一并占死。
- **`rh_zimage_app` 续跑接口缺失（严重）**：它是 App 通道，但没提供 `IS_APP_CHANNEL` / `check_app_task` / `extract_outputs` / `download_output`。导致其 taskId 被拿去查 `/proxy/history`，永远查不到 → 误判 `not_found`；即便远端有产物也拉不回来。主力方案 `zimage_i2i` 受影响。
- **ai-app 错误码 `807` 未识别**：`APIKEY_TASK_NOT_FOUND`（任务号不存在/过期/写错）此前被当成网络异常，任务卡死。现归类 `not_found`。
- **`301` 是复用码，必须按 msg 分流**：`taskId must be positive`（参数脏数据，重试无用）与 `must not be blank`（Key 为空，可重试）此前被一视同仁，前者会让任务永远卡在「进行中」。
- **App 通道无法走完「询问重提」**：`_inspect_task` 对 App 通道提前 `return`，导致即使识别出 `not_found` 也不会挂起询问，任务停在 `submitting`。

### 技术
- 契约 B（`ai-app/run`）模块新增硬性要求：`IS_APP_CHANNEL = True` + `check_app_task` + `extract_outputs` + `download_output`，缺任一项都会退化成「拿 taskId 查 /proxy」。
- `LORA_ALIAS` 简化为「短名 → 文件名」，强度改由 `LORA_DEFAULT_STRENGTH` 按 App 取，避免两处维护。
- 三份 `webui.html` 与两份 `rh_webui.py` / `rh_krea2_app.py` / `rh_zimage_app.py` 已重新对齐 MD5。

### 文档
- `runninghub-webui-devdocs` §6：补 A/B 两契约 pid 语义差异、App 通道必备接口、失败不自动重提规则、续跑注入陷阱、错误码补充。
- `runninghub-recon-api`：补 `807` 与 `301` 分流；标注 `userInfo` 已失效（实测 6 个变体全 404，`/api/user/info` 需网页 Token）；**新增国际站/国内站绝对不可混用警示**（两套独立账号，`.ai` 不可用时宁可关闭功能也不切 `.cn`）。

## [0.5.3] - 2026-10-18
### 新增
- **OpenAI 兼容接口（`scripts/rh_openai.py`）**：第三方软件（SillyTavern 等）可直接按 OpenAI 协议调用出图，无需了解 RunningHub 自有 API。
  - `POST /v1/images/generations`：文生图，支持 `prompt` / `size` / `n` / `seed` / `steps` / `denoise` / `lora` / `response_format`（url | b64_json）/ `async`（异步任务模式）
  - `GET /v1/models`：返回 `zimage` 模型 + `server_base`（本机与局域网地址自动发现）
  - `GET /v1/images/generations/<task_id>`：异步任务轮询
  - `GET /v1/files/<name>`：产物静态访问
  - `size` 按面积最近 + 宽高比方向匹配 zimage 的 6 档画布（768×1368 / 1080×1920 / 1242×2688 / 1024×1024 / 1368×768 / 1920×1080）
  - 强制鉴权：`Authorization: Bearer <openai_key>`，兼容 `api-key` / `x-api-key` 头、大小写差异、Bearer 前缀大小写、引号包裹、前后空格
  - `url` 跟请求 `Host` 头动态生成：同设备用 `127.0.0.1`，同内网用内网 IP，无需手工配置
- **API 出图自动登记 WebUI 历史**：`/v1/images/generations` 产物与网页出图共用 `webui_out/` 目录和 `TASKS` 表，历史面板新增「API 出图」分组，可预览、删除、保存到相册。
- **设置页 OpenAI 配置区块**（设置 → 后端 → OpenAI 兼容接口）：展示 Base URL（同设备/同内网，一键复制）、完整 API Key（可查看/复制）、模型名；支持重新生成 Key 与自定义 Key。
- **`GET /api/openai_key`**：返回完整 key，仅本机可访问。
### 安全
- **撤销 `/v1/chat/completions` 透传**：此前的 LLM 透传会把用户本机配置的私有 LLM Key 借给任何持有 `openai_key` 的人（同内网即可），等于替他人付账。该端点现返回 404 + 明确提示，对话请让客户端直连自己的 LLM 服务。`/v1/models` 也不再列出上游 LLM 模型。
- **`/api/*` 限制本机访问**：电脑版监听 `0.0.0.0`，此前 `/api/polish`（消耗用户自有 LLM 额度）、`/api/settings`（读写配置）等接口对同内网完全开放。新增 `_is_local_req` / `_deny_nonlocal` 校验，非回环来源一律 403。`/v1/*` 保持对内网开放（走 `openai_key` 鉴权），App 内嵌版绑 `127.0.0.1` 天然不受影响。
### 修复
- **鉴权 header 大小写**：`rh_webui` 把 `self.headers` 转成普通 dict 后丢失了 `email.Message` 的大小写不敏感特性，部分客户端发全小写 `authorization` 时鉴权恒失败（日志脱敏后两边看起来完全一致，难以定位）。新增 `_hget` 做大小写不敏感查找；诊断日志增加 key 长度与首个差异位置。
- **`rh_openai` 目录固化**（安卓端致命）：`OUT_DIR` / `CONFIG_FILE` 在模块级用 `BASE` 算好，而 `serve.py` 只覆盖了 `rh_webui` 的同名常量，导致 API 产物写入只读的 AssetFinder 目录、且读不到 `api_key.json`。改为运行期从 `rh_webui` 动态取（`_out_dir()` / `_cfg_file()`）。
- **`_webui()` 模块名判断**：只查 `sys.modules['rh_webui']`，命令行 `python3 scripts/rh_webui.py` 启动时模块名是 `__main__`，会静默回退到错误兜底路径。改为两种都认（用 `TASKS` + `OUT_DIR` 特征校验）。
- **弹窗内容过长无法滚动**：`#setModal` 只有垂直居中，没有 `max-height` / `overflow`，加入 OpenAI 区块后内容超高被挤出屏幕且不可滚动。加 `max-height:88vh` + `overflow-y:auto`。
- **历史落盘误删保护**：`_save_state()` 在独立进程里被调用时（内存 `TASKS` 为空）会把磁盘上的真实历史覆盖成空。新增保护：内存为空但磁盘非空时拒绝写盘并告警；同时改为「先写 `.tmp` 再 `os.replace`」的原子写，避免写一半损坏。
- **`/v1/files/` 双目录兼容**：产物目录从 `openai_out` 迁到 `webui_out`（与历史共用），旧产物仍可通过原目录访问。

## [0.5.2] - 2026-10-16
### 新增
- **ReActor 只换脸（`reactor_swap`）**：像素级换脸，只替换源视频里的人脸，身体/动作/服装/背景原样保留。走平台托管工作流（workflowId `2110722883786321922`，14 节点，`getJsonApiFormat` 实时拉取）。
  - 参数：参考脸（image）/ 源视频（video）/ 最长边上限 / 对比版开关 / 脸部修复强度 / 补帧倍率
  - **分辨率自动跟随源视频**：`ffprobe` 探测源视频宽高 → 等比缩放到 `max_side`（512/768/1024/1280）→ 输出严格保持源比例（实测 1280×2772 → 473×1024，720×1280 → 576×1024）
  - **解除 `frame_load_cap=90` 帧上限**：工作流写死只加载前 90 帧（>3.7s 视频被截断，实测 30s 只出 15s 产物）。改为 1500 帧上限，30s/720 帧源全量处理
  - **修复慢动作 bug**：工作流默认 `SimpleMath = 源fps/2` 导致输出帧率砍半（30s 源变 60s 慢动作产物）。改为 `VideoCombine` 直接引用 `VHS_VideoInfo` 源帧率，时长恢复等于源视频
  - 补帧机制说明：「关键帧换脸 + RIFE 补帧」，默认 2×（抽一半帧换脸 + 补回），关闭会顿挫
  - 音轨保留（aac），产物挂 `images` 数组但文件名 `.mp4`（平台实测，按扩展名过滤）
### 技术
- 平台接口响应格式不统一（实证归档）：`getJsonApiFormat` 带 `code`，`/proxy/prompt` / `history` / `upload` 无 `code` 字段，一律只在 `code` 存在且非 0/200 时判错
- `_inject_api_key` 模块列表加入 `rh_reactor`，避免 key 注入遗漏

## [0.5.1] - 2026-10-15
### 重构
- **素材槽位模型（替换全局 CUR_REF）**：旧设计用「全局单例 + 双向同步」，遇到一个脚本多个 ref 参数（如 wan22 的参考图+源视频）会互相覆盖——② 选一张图会把④ 的源视频也填成该图。新模型把绑定状态按「脚本 + 槽位 key」存（`SLOTS["wan22_animate.ref_video"]`），槽位之间完全独立。
  - ④ 参数里的 ref 渲染成**槽位卡片**（缩略图 + 类型标签 + 文件名 + 说明），点击开素材库浮层，再点解绑
  - 素材类型由槽位声明 `accepts: image|video` 决定，不再靠 `type==='ref_video'` 硬编码
  - ② 改为**素材库入口**（浏览/上传/删除），不再有全局选中态
  - 删除 `CUR_REF`、`pickRef` 全局同步、`submit()` 里 `ref_image:CUR_REF` 硬编码
  - 给 `zimage_i2i` / `krea2_i2i` 补 `ref_image` 参数声明（此前靠前端兜底）
  - 上传不再自动绑定槽位（旧行为会污染其他槽位）
### 新增
- **Wan Animate 2.2 动作迁移（`wan22_animate`）**：参考图给身份 + 源视频给动作，产出「参考图人物复刻源视频服装/动作/姿态」的视频。走平台托管工作流（workflowId `2110720306189381634`），用 `getJsonApiFormat` 实时拉取，不依赖本地打包 JSON。
  - 参数：参考图 / 源视频 / 分辨率（480×832 · 544×960 · 640×640 · 832×480）/ 人脸增强 / 对比图开关 / 步数 / 种子
  - **默认只出 1 份纯净产物**（480×832 尺寸精确命中设定，带音轨）；勾选「对比图」才额外出拼接对比版
  - 通用预置提示词：留空时自动套用「复刻示例视频的服装/动作/姿态，身份来自参考图」
- **参考素材九宫格分页**：单页 9 个（3×3），顶部分页器 `1-9 / 35` + `‹ 1/4 ›`。素材图 / 参考视频 / 出图产物 三栏页码独立。
### 修复
- **视频缩略图破图**：`refThumb` 原本无条件用 `<img>`，导致 mp4 加载失败变成半透明灰块。改为按扩展名分流，视频用 `<video>`。
- **参考图栏混入视频**：新增「参考视频」独立栏目，素材图栏只显示图片、出图产物栏只显示图片，视频（素材视频 + 产物视频）集中在参考视频栏。
- **任务破图（删除产物后）**：`/api/tasks` 返回前按磁盘校验 `image`/`files`，文件已删则置空并回退到首个存在的产物；前端 `<img onerror>` 兜底显示「🗑 产物已删除」。
### 改进
- **视频懒加载**：`preload="none"` + IntersectionObserver，进入视口才拉首帧（mp4 的 moov 在文件尾部，35 个视频同时预载会拉约 50MB）。
- **`_send_file` 支持 Range（206）**：视频拖动进度/断点续传必需，此前一律 200 全量返回。

## [0.5.0] - 2026-10-14
### 新增
- **出图即背景（液态玻璃外观）**：设置 → 🎨 外观。把最近一张完成的出图作为页面背景，跑出新图自动替换；玻璃拟态卡片（backdrop-filter + 渐变高光）压在上面。蒙版/玻璃透明度/背景模糊三滑杆可调，localStorage 持久化。后端新增 `/api/latest_image` 接口。
- **设置面板新增「🎨 外观」Tab**：开关（出图即背景 / 玻璃拟态卡片）+ 蒙版/玻璃透明度/背景模糊调节 + GitHub 项目源码链接。
- **启动更新检测**：打开 app 自动查 GitHub 最新 Release，有新版弹 toast 提示（15 分钟缓存；走 302 重定向解析版本号，绕开 GitHub API 限流）。
- **顶部按钮分色**：历史任务（金）/ 设置（蓝）/ 应用市场（紫）三色区分；「⚙ 密钥」改名「⚙ 设置」。
### 修复
- **尾帧破图（中文文件名 404）**：后端 `/refs/`、`/images/` 路由补 `urllib.parse.unquote`，前端 `encodeURIComponent` 编码的中文文件名（如「尾帧_*.jpg」）能正确解码找到文件。
- **上传参考图按钮样式重做**：hint 文字 + 原生 input + 按钮 三件套 → 统一点击式上传卡片（虚线卡片、图标、文件名显示、金色 ready 态、上传中禁用）。
### 改进
- **产物不再自动进相册**：删除 `_run_task` / `_download_from_entry` 里 6 处自动导出调用，产物只存 app 内部（workflows/webui_out）；长按任务 → 💾 保存到相册 手动导出。
- **轮询改为任务驱动**：有活跃任务才 10s 轮询（完成即停），不再"抽屉开着就一直后台刷"；出图完成自动刷新背景，无需手动刷新页面。

## [0.4.9.1] - 2026-10-14
### 修复
- **App 内参考图上传无反应**：Android WebView 缺 `WebChromeClient.onShowFileChooser` 回调，`<input type="file">` 弹不出系统文件选择器。补上文件选择回调 + `onActivityResult` 处理 + 泄漏防护（仅安卓壳改动，需重装 APK）。

## [0.4.9] - 2026-10-14
### 新增
- **抽取尾帧（长按任务菜单）**：长按历史任务卡片 → 🎞 抽取尾帧。视频任务自动抽取最后一帧存入素材库（refs/）并设为当前参考图，可直接拿去做图生图首帧。双环境抽帧：本地走 `ffmpeg -sseof`，安卓走系统 `MediaMetadataRetriever`（新增 `VideoFrames.kt`，无需权限）。
- **复用提示词（长按任务菜单）**：长按历史任务卡片 → 📋 复用提示词。把该任务的脚本、提示词与全部参数（含 LoRA 勾选/强度、参考图）一键回填到表单，修改后手动提交，不消耗点数。
### 修复
- **Krea2 LoRA 全关时节点悬空**：LoRA 全部关闭时，下游节点仍硬编码引用已跳过的身材 LoRA 节点（200）导致报错，改为按当前末端节点（`cur_model`）动态串联。
- **App 内参考图上传无反应（hotfix）**：Android WebView 缺少 `WebChromeClient.onShowFileChooser` 回调，`<input type="file">` 弹不出系统文件选择器；补上文件选择回调 + `onActivityResult` 处理 + 泄漏防护。
### 改进
- 长按菜单「↻ 再次提交」更名为「↻ 原样重跑」，与「📋 复用提示词」区分（前者直接重跑扣币，后者回填不扣币）。

## [0.4.8] - 2026-10-14
### 新增
- **Illustrious 二次元文生图脚本（T2I）**：WAI-illustrious-SDXL 底模 + 可勾选 LoRA（Smooth_Booster 画质增强 / Cunnystyle 插画微调），半写实 CG 动漫风。封装平台工作流 2050930923734093825
- **zanime 真人转二次元 LoRA**：zimage 脚本新增 `zanime` 选项（Z-Anime04，选图生图 + 勾选即真人转二次元），实测 3 档 denoise 均可出图

### 改进
- **LoRA 勾选框样式重做**：自定义金色勾选块放在标题前，说明独立换行，强度靠右，替换原生 checkbox
- **Illustrious LoRA 命名与默认修正**：Cunnystyle 明确为「插画微调」（底模已是二次元，非转换用途），默认不勾选

## [0.4.7] - 2026-10-13

### 重构

- **单源维护（消灭三处同步）**：本地 WebUI 与安卓 app 共用同一份代码，不再维护多副本。`BASE` 改为三路自适应（`RH_BASE` 环境变量优先 → `__file__` 推导 → app 由 `serve.py` 覆写为 filesDir）；`webui.html` 端口改为 `__API_PORT__` 占位符，由后端按实际监听端口注入（本地 8770 / app 8765）。改代码只改仓库一份 → push → CI 自动出 APK，本地直接跑同一份。

### 新增

- **错误查询（长按任务菜单）**：长按历史任务卡片 → 🔍 错误查询 / 💾 保存到相册 / ↻ 再次提交 / 🗑 删除。错误查询全自动闭环：
  - 有 pid → 查远端 `/history/<pid>`：已成功 → 拉回产物落盘；还在排队/生成 → 自动恢复轮询（出图自动落盘）；远端失败 → 显示真实错误。
  - 无 pid → 自动从 `/queue` + `/history` 认领远端任务 pid；认领不到 → 自动重新提交（同参数，新 pid 接管），全程只点一次。
- **pid 持久化**：提交成功立即写入任务状态（`on_submit` 回调），app 被杀/重启后 pid 不丢，可用错误查询续跑。
- **排队超时不判死**：轮询超时从 `RuntimeError` 改为专用 `PollTimeout`，任务状态设 `poll_timeout`（🕐 排队超时），保留 pid 等恢复；轮询上限 5~15 分钟统一提到 30 分钟。
- **产物导出公共目录**：产出自动导出 + 手动保存到相册。图片 → `Pictures/RunningHub`（相册直接可见）、视频 → `Movies/RunningHub`、其它 → `Download/RunningHub`，通过 MediaStore 无需任何存储权限。

### 修复

- **排队超时的任务不落盘显示破图**：排队 20 分钟超过轮询上限被误判失败，远端实际成功但产物无人拉取。现在 pid 全程落盘 + 超时不判死，随时可用错误查询拉回。
- **`urllib.request.Request(timeout=)` 非法调用**（烧币级）：`Request.__init__()` 不接受 `timeout`，仅 `urlopen()` 支持。原代码 `_check_pid_status` 用 `Request(f".../history/{pid}", timeout=30)` 每次必抛 `TypeError` → 永远 `api_error` → 旧逻辑把 `api_error` 当 `not_found` → **自动重提重复烧币**。已删除非法 timeout，查询失败一律返回"请稍后重试"，绝不自动重提。
- **假活任务占死提交锁**：服务重启后轮询线程被杀，任务状态停在 `submitting` 但远端早已完成/不存在，导致提交按钮永远被锁。新增 `_recover_stale_tasks()` 启动时自动复核：已完成 → 拉回落盘 / 还在跑 → 续轮询 / 远端不存在 → 标失败 / 查询失败 → 保持原状，绝不自动重提。
- **连点提交并发多任务**：前端 `SUBMIT_LOCK` 整段运行期锁 + 后端 `_has_active_task()` 硬锁，双保险，普通账户单任务约束。
- **卡片旧按钮样式残留**：历史任务卡片 `↻`/`✕` 小按钮移除，统一走长按菜单。
- **本地 webui "后端未连接"**：webui.html 写死 `8765` 端口导致本地 8770 连不上，改为端口注入后根治。

## [0.4.6] - 2026-10-13

### 修复

- **应用市场搜索无效**：后端把关键词放进了 `body["extra"]["keyword"]`，但 RunningHub `/api/webapp/list` 的搜索字段名是 `search`（实测 13 种参数仅 `search` 生效）。已改为 `body["search"]`。
- **关闭 app 后历史记录全部丢失**：`_load_state()` 原在模块级调用，此时 `BASE` 尚未被 `serve.py` 覆盖为 filesDir，读的是不存在的 `AssetFinder/workflows/webui_state.json`，而 `_save_state()` 写入的却是 filesDir → 读写路径不一致。改为在 `BASE` 指向运行时目录后再调用（安卓在 `serve.py`、本地在 `__main__`）。

## [0.4.5] - 2026-10-13

### 修复

- **app 所有联网功能失效（点数查询 / 应用市场 / 跑图 / LLM 润色全部报"未连接"）**：根因是 `rh_webui.py` 模块级把不存在的 `AssetFinder/scripts` 目录插入 `sys.path`。Chaquopy 的 importer 在 `import urllib.request`（→`http.client`→`socket`→`_socket` C 扩展）时会遍历 `sys.path`，撞上这个幽灵路径就抛 `FileNotFoundError`，导致所有需要访问 RunningHub 外网的请求全部失败。改为仅在目录真实存在时才插入 `sys.path`；`serve.py` 额外做一次兜底清除。

## [0.4.4] - 2026-10-13

### 修复

- **app 主页"后端未连接"**：v0.4.2 改为"后端未就绪也加载页面"导致冷启动时页面先于后端渲染、API 请求失败。恢复"后端就绪后才加载页面"，waitForServer 超时 30s→90s，前端 api() 增加启动期自动重试兜底。

## [0.4.3] - 2026-10-13

### 新增

- **设置 → 后端 tab**：显示后端运行状态（PID/端口/Python 版本/启动时间/任务数），支持手动重启后端。
- **后端状态 API**：`GET /api/backend` 返回运行状态；`POST /api/backend/restart` 重启服务（本地 spawn 新进程，安卓端提示重开 app）。

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
