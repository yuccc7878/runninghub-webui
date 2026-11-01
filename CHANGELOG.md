## [0.5.18] - 2026-11-02

全量代码复查 + 双端同步 + 版本号统一。

### 版本号统一到 0.5.18

此前四处不一致：`scripts/rh_webui.py` 的 `CUR_VERSION` 停在 `0.5.15`，
而 `webui.html` / `android/.../webui.html` / `build.gradle` 是 `0.5.17`。
`CUR_VERSION` 用于「检查更新」比对 GitHub Release，落后会导致误报有新版本。

| 位置 | 改前 | 改后 |
|---|---|---|
| `scripts/rh_webui.py` `CUR_VERSION` | 0.5.15 | 0.5.18 |
| `webui.html` meta / title / h1 | 0.5.17 | 0.5.18 |
| `android/app/build.gradle` `versionName` | 0.5.17 | 0.5.18 |
| `android/app/build.gradle` `versionCode` | 30 | 31 |

### 双端同步（这是本轮主要问题）

`android/app/src/main/python/` 与 `android/app/src/main/assets/` 长期落后，
上一轮所有改动（账号通道、断 key 开关、HIDDEN_SCRIPTS）都没同步进去，
**打包出来的 APK 是旧逻辑**。

已同步 4 个文件，同步后 17 个共用文件 md5 全部一致：

- `scripts/rh_webui.py` → `android/app/src/main/python/rh_webui.py`
- `scripts/rh_zimage_api.py` → `android/app/src/main/python/rh_zimage_api.py`
- `webui.html` → `android/app/src/main/assets/webui.html`（MainActivity 实际加载这份）
- `webui.html` → `android/app/src/main/webui.html`（冗余副本，git 跟踪但无引用）

### EMBEDDED_SCRIPTS 离线兜底表重生成

`webui.html` 内嵌的离线参数表与 `REGISTRY` 漂移：

- `h3_omni_video` 是旧配置（有已废弃的 `prompt_text` 字段，缺 `ratio` 规范）
- 4 个条目的 `hint` 被序列化成字符串 `'None'`

用 `scripts/_sync_embedded.py` 重新生成，现与 `REGISTRY` 逐字段一致（8 个 key 全对齐）。

### 复查结论

- 85 个 py 文件语法全部通过
- `REGISTRY` 引用的 8 个模块 + `rh_webui` 的 5 个 import 依赖，安卓侧均存在
- 85 个文件中有 68 个是 `_` 开头的临时调试脚本（未同步到安卓，属正确行为）
- `upload_refs.py` / `patch_webui_h3dual.py` / `inspect_embedded.py` 三个一次性
  工具脚本没有 `if __name__ == "__main__"` 保护，import 时会执行真实上传。
  不影响 webui 运行，但如果以后做自动扫描要注意。

### 回归验证（v0.5.18）

| 项 | 结果 |
|---|---|
| 启动 | 正常，6 脚本 / 31 参数 |
| 余额 | `🪙 RH币 23590 · 账号通道`（session 通道） |
| `apiKeyBlock` | `display: none` |
| 隐藏脚本 | `reactor_swap` / `illustrious_t2i` 提交均 400 |
| OpenAI zimage | 1024×1024 PNG，App 2112630365772128258，4 字段 |
| OpenAI krea2 | 1024×1024 PNG，App 2113009215693500417，4 字段 |
| OpenAI moss | 1024×1024 PNG，App 2114331355738923010，4 字段 |
| 网页提交 | `zimage_i2i` → `T036_1793564439` → done |

## 2026-11-02 · 暂隐两个未完成 App 改造的脚本

- **新增 `HIDDEN_SCRIPTS = {"reactor_swap", "illustrious_t2i"}`**（`scripts/rh_webui.py`）
  - `reactor_swap`：走 `/proxy/<key>/prompt`，与「断开 API Key 通道」直接冲突，无 session 等价接口
  - `illustrious_t2i`：尚未完成 App 通道改造，从未跑过真实任务
  - 代码、REGISTRY 配置、模块文件全部保留。从集合里删掉 key 即恢复。

- **三道拦截**
  1. `_visible_registry()` → `/api/scripts` 不再下发这两个 key
  2. `/api/submit` → 返回 400「已暂停使用」
  3. `_run_task` 开头兜底 → 覆盖 `/api/task/resubmit` 和启动时 stale 恢复两条隐蔽路径

- **前端兜底过滤**（`webui.html` `init()`）
  服务端不可达时会退回 `EMBEDDED_SCRIPTS` 离线常量，里面仍有这两个，
  在 `init()` 里统一 `delete`，避免断网兜底时露出来。

- 历史任务分组（`reactor_swap2`）保留不动，属于历史数据。

验证：`/api/scripts` 返回 6 个脚本；两脚本提交均 400；
历史 reactor 任务重跑被拦为 `failed`，未发起真实请求。

## [0.5.24] - 2026-11-02
### 新增
- **MOSS 支持精确自定义分辨率**（此前只能输出固定比例下的 1440×1440）。
### 原理（实测 2026-11-02）
画布结构：
```
23 ResolutionSelector(width,height)
   ├─ width  (link 7) → 6 EmptyFlux2LatentImage.width
   └─ height (link 8) → 6 EmptyFlux2LatentImage.height
```
`6.width/height` 的输入**有连线**，App 发布时也没暴露这两个字段，
但把它们写进 `nodeInfoList` 平台仍会采用，且**优先级高于 `23.aspect_ratio`**：
| 测试 | 提交 | 实际输出 |
|---|---|---|
| C | `6.width=832, 6.height=1216`（ratio=1:1） | **832×1216** |
| D | `ratio=9:16` + `6.width/height=1024/1024` | **1024×1024**（直传胜出） |
| B | `23.megapixels=1`（ratio=1:1） | 1024×1024 |
所以不必改工作流、不必重新发布 App，直接传 `6.width/height` 即可精确控制像素。
### 改动
- `rh_moss_app.build_node_info()` 新增 `width` / `height` 参数：
  传了就写 `6.width/height`（新增 `add_raw()`，跳过暴露白名单校验），
  不传则维持原有 `23.aspect_ratio` 逻辑。
- `rh_moss_app.NODE` 新增 `"latent": "6"`。
- `rh_moss_app.run()` 签名新增 `width` / `height`，日志区分「精确尺寸」和「比例折算」两种模式。
- `rh_openai.MODEL_SPECS["moss"]`：`supports.width/height` 改为 True，
  `needs_ratio` 改 False。`_run_via_app()` 调整为宽高优先、ratio 仅作后备。
### 实测（走 OpenAI 接口，api_key 为空）
```
POST /v1/images/generations
{"model":"moss","prompt":"...","size":"1024x1024","n":1,"steps":8}
→ data[0].url = moss_zip_00006_hnrxq_1793563328.png
```
- 输出尺寸 **1024×1024** ✅
- 提交字段：`4.text, 16.steps, 6.width, 6.height`
- 日志：`[参数] 精确尺寸 1024×1024 | steps=8`
### 说明
- MOSS 的 `size` 现在与 zimage / krea2 行为一致，三个模型都支持精确像素。
- `ratio` 参数仍保留：不传 size 而传 `ratio` 时走原枚举路径。
- 注意：`6.width/height` 属于「隐性可用字段」——App 发布界面看不到，
  平台若变更填充规则可能失效。失效时的表现是回退成比例默认值，
  不会报错，需靠出图尺寸核对。

## [0.5.23] - 2026-11-02
### 新增
- **OpenAI 兼容层支持 3 个模型**（全部走 App 通道，不需要 API Key）：

  | model | App | 支持参数 | 别名 |
  |---|---|---|---|
  | `zimage`（默认） | 2112630365772128258 | size、steps | `zimage-t2i` / `zimage_t2i` |
  | `krea2` | 2113009215693500417 | size、steps | `krea2-t2i` / `krea2_t2i` |
  | `moss` | 2114331355738923010 | ratio（由 size 折算）、steps | `moss-t2i` / `moss_t2i` |

  - `GET /v1/models` 会列出三个模型，`description` 里标注各自的 App 和可传参数。
  - 模型名写错时，错误信息会列出全部可用模型。
- **`size` → `aspect_ratio` 折算**（MOSS 专用）：MOSS 的 App 只暴露 8 档比例枚举，
  没有数值宽高。新增 `size_to_ratio()` 按 |w/h − 候选比例| 取最接近的一档。
  也可以直接在请求里传 `"ratio": "16:9 (Widescreen)"` 精确指定。
### 修复
- **`rh_openai._run_one` 没把 `steps` 传给 App**。App 的 `4.steps`（KSampler）
  是暴露字段，之前漏传导致始终用默认值 6。现已接上，走 `extra["steps"]`。
- `model` 参数现在贯穿 `generate → _run_sync/_worker → _run_one` 全链路。
  之前只有 `generate` 校验机型，下游不知道目标模型。
### 实测（api_key 为空 / 走账号通道）
| 模型 | 请求 | 实际产物 | 提交字段 |
|---|---|---|---|
| zimage | 1024x1024 | 1024×1024 PNG | 5.text, 16.value, 17.value, 4.steps |
| krea2 | 1024x1024 steps=10 | 1024×1024 PNG | 41.text, 39.value, 40.value, 36.steps |
| moss | 1024x1024 | **1440×1440**（见下） | 4.text, 23.aspect_ratio, 16.steps |
- 别名 `krea2-t2i` 正常归一。
- 未知模型返回 400 + 可用列表。
### 已知限制
- **`seed` 全模型不支持**：三个 App 的 `inputNodes` 都没暴露 KSampler 的 seed，
  传了会被忽略，无法复现同一张图。
- **MOSS 输出尺寸不受 `size` 精确控制**：App 内 megapixels 锁定 2.0，
  `1024x1024` 只用于折算比例，实际出 1440×1440。需要精确像素请用 zimage / krea2。
- `illustrious` 未接入：该模块走 `/proxy` 工作流通道（需 API Key），与"断 key"目标冲突。
- Krea2 的 3 组 LoRA 强度（28/29/30）可传但当前未在 OpenAI 接口暴露，
  如需可后续加 `lora_*` 参数。

## [0.5.22] - 2026-11-02
### 变更
- **OpenAI 兼容层（`/v1/*`）从 API Key 通道切换到 App 通道**。
  新增开关 `OPENAI_USE_APP_CHANNEL = True`（`rh_openai.py`）。
  - `True`  → `rh_zimage_app.run_t2i()`（App 2112630365772128258，账号/session）
  - `False` → `rh_zimage_api.run()`（`/proxy` 通道，需 API Key，代码保留）
- `_inject_key()` 加了 `API_CHANNEL_DISABLED` 判断：开关打开后不再从配置读 key，
  避免绕过 `rh_webui` 的断开开关。
### 修复
- **App 通道直出 zip 导致 `/v1/images/generations` 返回不可用链接**。
  新增 `_unzip_first_image()`：产物是 `.zip` 时解出第一张图再返回 URL，
  符合 OpenAI 规范（原来会返回 `...zip`，第三方客户端无法渲染）。
### 实测（api_key 清空状态下）
- 调用 → `{"data":[{"url":"http://127.0.0.1:8770/v1/files/zimage_zip_00001_glnea_1793561394.png"}]}`
- 下载：HTTP 200，1,002,875 B，PIL 校验 768×1368 RGB PNG。
- 日志确认走 App：`[App] 2112630365772128258 (文生图) 提交 4 个字段`。
- 说明：`/proxy` 通道产物文件名形如 `ComfyUI_xxxxx.png`，App 通道形如 `zimage_zip_xxxxx.png`，
  可据此区分实际走的哪条路。
### 已知差异（App 通道能力弱于原 /proxy 通道）
- `seed` / `denoise` / `steps` 在该 App 未全部暴露，传入会被忽略。
- `n>1` 由上层循环多次调用实现，行为不变。

## [0.5.21] - 2026-11-02
### 变更
- **API Key 通道断开（只断开关联，代码全部保留）**。新增总开关 `API_CHANNEL_DISABLED = True`
  （`rh_webui.py:52` 附近），把它改回 `False` 即恢复原行为。
  - `_get_api_key()` 在断开时恒返回 `""`，等价于“从未配置”。
  - `POST /api/settings` 收到 `api_key` 时**静默忽略**，不报错也不写入。
  - `workflows/api_key.json` 里的旧值**不删除**，只是读不到，日后恢复可直接用。
  - 前端 `#apiKeyBlock` 默认 `display:none`，顶栏不再显示「🔑已设/未设」。
### 保持不变
- `llm_api_key`（AI 润色）与 `openai`（OpenAI 兼容层）是**独立密钥**，未受影响。
- 余额查询、任务提交、素材上传均走登录账号（session），与本开关无关。
### 实测（开关为 True）
- `/api/settings` → `api_key_set: False, masked: ''`。
- 尝试写入测试 key → 返回 `{"ok": true}`，但再读仍为 `False`，磁盘旧值仍为 32 位。
- 图生图真实任务：taskId `2116969503372169218`，产物 `krea2_zip_00001_lzajk_1793559868.png`，status `done`。
- 启动日志只有「通道: 账号（session，已接管 21 处请求）」，无 key 提示。
- 页面：`verInfo` 无钥匙徽标，`apiKeyBlock` = `display:none`，余额「🪙 RH币 23569 · 账号通道」，0 报错。
- 副本 md5 一致（webui.html ×3、rh_webui.py ×2）。

## [0.5.20] - 2026-11-02
### 修复
- **视频任务的源视频上传漏包（严重）**：`_make_upload_shim` 只覆盖了 `upload_image`，
  而 `rh_wan22` 的 `upload_video` → `upload_media(p, "video")` 没被覆盖，
  仍会把 `apiKey` 写进 multipart body。断掉 API Key 后表现为
  `HTTPError: HTTP Error 400`（参考图上传成功、视频上传失败）。
  - 挂载点从单个 `upload_image` 扩展为 `("upload_image", "upload_video", "upload_media")`。
  - shim 内部改调 `rh_session_channel.upload_media()`（新增），
    兼容 `upload_media(path, file_type)` 的位置参数。
- **新增 `rh_session_channel.upload_media()`**：通用素材上传，
  content-type 按扩展名推断（`.mp4`→`video/mp4`，`.mov`→`video/quicktime` 等），
  避免视频被当 `octet-stream` 上传。
### 实测（api_key 清空状态下，三个视频脚本全通）
| 脚本 | taskId | 产物 | 校验 |
|---|---|---|---|
| `h3_flf_video` 首帧 | 2116941707010732034 | `clean_zip_00001_cycqz_1793553275.zip` | h264 480×864 + aac，5.17s |
| `h3_omni_video` 双参考 | 2116960030821801986 | `clean_zip_00001_tbkjo_1793557708.zip` | h264 480×864 + aac，5.17s |
| `wan22_animate` 动作迁移 | 2116961011311661058 | `wan_zip_00001_kzybu_1793558139.mp4` | h264 480×832 + aac，4.71s |
- 首帧/双参考的参考图均走 session 上传，日志可见 `first.png → 687f378d...png`
  （裸 md5 文件名，非 `api/xxx.png`），证明确实走账号通道。
- Wan 的源视频走 session 上传后提交成功，产物为可直接播放的 mp4。
### 已知限制
- **`reactor_swap`（ReActor 换脸）仍走 API Key**：其 `_req` 命中
  `/proxy/<key>/prompt`，是 API Key 专属通道，无 session 等价接口。
  断 key 后报 `RuntimeError: 未设置 API Key`。尚未改造为 App 通道，暂时跳过。

## [0.5.19] - 2026-11-02
### 变更
- **webui 全链路切换为登录账号（session）通道，API Key 不再使用**。
### 新增
- **余额查询改走账号**：`GET /api/balance` → `POST /uc/point/summary`
  （鉴权 `Authorization: <Rh-Accesstoken>`，裸 token 无 Bearer）。
  - 字段坑：`billingVersion=OLD` 时真实余额在 `transitionCoinRemained`，
    `balancePoint`/`paidPoint`/`bonusPoint` 全为 0。后端已做择优，
    同时保留 `remain_coins`（兼容旧前端）与 `balance_point`。
  - 与原 API Key 版（`/uc/openapi/accountStatus`）是**两套独立账本**：
    实测账号通道 23,694，API Key 通道 5,137（分属主号与测试号）。
- **参考图上传改走账号**：新增 `_make_upload_shim()`，包装各模块的 `upload_image`。
  - 原实现把 `apiKey` 硬编码进 multipart body，走 `/task/openapi/upload`，
    停用 API Key 后图生图/首帧/动作迁移全挂。
  - 新链路走 `rh_session_channel.upload_image()` 的 `/upload/image`（三件套认证：
    `Authorization` + `Rh-Identify` + `Rh-Comfy-Auth`），返回裸文件名。
  - 实测：裸文件名直填 `LoadImage.image` 可被 App 接受（真实图生图任务已出图）。
### 修复
- **`_inject_api_key()` 在 key 为空时提前 return，导致 session shim 装不上**。
  这是切换账号后最致命的隐藏依赖：key 一空，提交/查产物/上传的 shim 全部不生效，
  业务链会静默退回到需要 API Key 的老路径。已改为无论 key 是否为空都继续装 shim。
### 移除
- 删除死代码 `_rh_web()`（市场删除后已无调用点）。
### 验证（api_key 清空状态下实测）
- 余额：`remain_coins: 23679`，`channel: session`。
- 图生图全链路：session 上传 → 提交 → 出图 → 解包 png，
  taskId `2116939922485370881`，产物 `krea2_zip_00003_tteqk_1793552792.png`。
- 页面：15 任务 / 7 缩略图 / 0 破图，余额栏显示「🪙 RH币 23676 · 账号通道」。
- 三份 `webui.html` 与 `android/.../rh_webui.py` 副本 md5 一致。
### 注意
- `api_key` 配置项保留但不再使用（启动时会打印提示）。
- 历史遗留的 `/proxy` 通道 pid 在无 key 时无法查询，已改为明确报错而非静默失败。
- 视频类脚本（H3/ReActor/Wan）本次未跑真实任务，但走同一套 shim 路径。

## [0.5.18] - 2026-11-01
### 移除
- **应用市场功能整体删除**（已不需要）。涉及三层：
  - 后端删除 4 个函数：`_market_list` / `_market_tags` / `_market_detail` / `_market_submit`
    及 `_MK_DETAIL_CACHE`（共 5153 字节）。
  - 后端删除 4 条路由：`GET /api/market`、`GET /api/market/tags`、
    `GET /api/market/detail`、`POST /api/market/submit`（共 1569 字节）。
  - 前端删除：顶栏「🛒 应用市场」按钮、`#marketMask` / `#marketPanel` 面板（含
    `#marketBody`）、对应 CSS 两条、以及整段 JS（`toggleMarket` / `loadMarket` /
    `renderMarketList` / `openMarketDetail` / `renderMarketDetail` / `submitMarketApp`
    与 `MK_*` 状态变量，共 179 行）。
- 三份 `webui.html` 副本与 `android/app/src/main/python/rh_webui.py` 已同步。
### 验证
- 市场端点全部 404：`/api/market`、`/api/market/tags`、`/api/market/detail`。
- 其余端点正常 200：`/`、`/api/scripts`、`/api/tasks`、`/api/balance`、`/api/refs`、`/api/settings`。
- 页面无 JS 报错；`toggleMarket` / `loadMarket` 均为 undefined（已彻底移除）。
- 顶栏无市场按钮、DOM 无 `#marketPanel`；8 个脚本、13 个历史任务、5 张缩略图、0 张破图。
- 全仓 grep `market|应用市场|MK_PAGE|_market_` 零命中。
- `rh_webui.py` 语法校验通过。
### 未动
- 鉴权白名单 `allowed` 本就不含市场端点，无需改。
- `_rh_web` / `_query_balance` 保留 —— 它们还被余额查询等功能使用。

## [0.5.17] - 2026-11-01
### 新增
- **zip 产物自动解包**。krea2 / zimage 等图片工作流改成 `CompressImages` 只出 zip 后
  （为了绕开平台下载管道加的水印和 AIGC 标记），webui 前端仍按图片渲染 zip，
  任务卡片和预览弹窗全是破图。现在在落盘层统一解包，前端拿到的仍是 png。
  - 新增 `rh_webui._unpack_zip_outputs()`，6 处调用点覆盖全部落盘路径：
    `krea2` / `zimage_i2i` / `moss_t2i` / `illustrious_t2i` 四个提交分支、
    `wan22_animate`（视频+zip 混合）、`_download_from_entry`（续跑/轮询路径）。
### 关键设计
- **必须重命名，不能直接用 zip 内成员名**。`CompressImages` 打包出来的成员名固定是
  `image_00000.png`，直接解压会让所有任务的产物互相覆盖。
  改为按 zip 主名重命名：`krea2_zip_00001_lisnc_1793483298.zip`
  → `krea2_zip_00001_lisnc_1793483298.png`。多成员时追加序号 `_00/_01`。
- **保留原 zip 文件**，但不放进返回列表。用户仍可在产物目录取原件（无水印版），
  前端也不会再把 zip 当图片渲染。
- **解包失败不判死任务**：原 zip 保留并返回，至少能手动下载。
- **22 字节空包**（`PK\x05\x06` + 18 个 0）回退保留 zip，让异常可见，不静默当成功。
- `wan22_animate` 解包后仍优先用 mp4 当封面，不会被 png 抢走。
### 验证
- 单元：两个 zip 同时解 → 各自独立文件名，无覆盖；混入 mp4 → 顺序保留；
  不存在的 zip → 返回空；22 字节空包 → 回退保留 zip。
- 产物有效性：解出的 png 能被 PIL 正常打开（1080×1920 / 768×1368 RGB），`im.info` 为空。
- 端到端：krea2 文生图实跑，产物 `krea2_zip_00001_buhnp_1793483978.zip`
  → 解包为 `krea2_zip_00001_buhnp_1793483978.png`，文件存在且可打开。
### 修复
- **版本号长期未同步**。`webui.html` 的 `<title>` / `<h1>` / `<meta name="version">`
  和 `android/app/build.gradle` 都停在 `0.5.15`，而 CHANGELOG 已记到 `0.5.17`
  （0.5.16 / 0.5.17 只改了 CHANGELOG 没升版本号）。三处统一到 `0.5.17`，
  `versionCode 28 -> 30`。三份 `webui.html` 副本同步。
- **`meta[name=version]` 多写了一个 v**，导致页头显示 `vv0.5.15`。
  改为不带前缀（`content="0.5.17"`），由 JS 拼 `v`。

## [0.5.16] - 2026-11-01
### 移除
- **webui 移除 Zimage 图生图功能**，该入口现在只能文生图。
  根因：图生图 App `2112645746440085506` 已从平台下线，
  `/api/webapp/detail` 返回 `901 WEBAPP_NOT_EXISTS`，这个入口提交必失败。
  - `REGISTRY["zimage_i2i"]`：删掉 `ref_image` / `denoise` / `lora_a` / `lora_b` 四个参数，
    `mode` 由 `i2i` 改为 `t2i`，名称改为「Zimage 文生图 (T2I · App)」。
  - 提交分支改调 `mod.run_t2i()`，不再走 `mod.run()` 的自动分流。
  - 历史任务若带 `ref_image` 重跑，直接抛错并提示改用 Krea2 ——
    不静默忽略，否则用户会以为参考图生效了。
  - `rh_zimage_app.run()` / `run_i2i()` 改为快速失败，不再发请求等 901。
### 保留
- **REGISTRY key 仍叫 `zimage_i2i`，不重命名**。历史任务的 `params` 里存的是这个 key，
  `rerunTask()` 靠它找回脚本，改 key 会让所有旧任务重跑失败。
- 文生图能力不变：App `2112630365772128258`，分辨率 / 步数 / 种子。
### 验证
- 注册表与三份 `webui.html` 的 `EMBEDDED_SCRIPTS` 离线兜底表参数项一致（`resolution/steps/seed`）。
- `run()` / `run_i2i()` 传 `ref_image_path` 均抛 `RuntimeError`。
- 文生图实跑通过：taskId `2116649873155907586`，产物 `zimage_zip_00001_zobcd_1793483667.zip`。
### 相关
- 同一轮另将 krea2 文生图工作流 `2112996948134440962` 改为只出 zip（V3 → V4），
  详见 `skills/runninghub-zip-no-watermark/SKILL.md` 7.4c2。

## [0.5.15] - 2026-10-30
### 变更
- **H3 首尾帧脚本整体替换为 App 版**（`rh_h3_flf_api.py` 重写，约 456 行）。
  从 Native `/proxy` 通道切到 `ai-app/run` 通道，对接已发布 App
  `2115566654334791681`「minimaxh3锁首帧最长30s加速生成版」。
  - 暴露字段（实测）：`114.image` 首帧图 / `115.megapixels` 分辨率 / `135.value` 时长 / `145.text` 提示词。
  - `IS_APP_CHANNEL=True` 通道标记，`check_app_task` / `extract_outputs` / `download_output` 齐备，
    `rh_webui` 的断点续跑逻辑无需改动。
  - `run()` 保留全部旧参数名并加 `**_ignored` 兜底，旧调用方式不会因为参数变少而报错。
  - 帧数由工作流内 `134 ComfyMathExpression` 自动换算，`17n+5` 对齐：
    5s→124 / 10s→243 / 15s→362 / 20s→481 / 30s→736。
- **参数从 8 项精简到 3 项**：首帧图、分辨率（0.4/0.6/0.9/1.0 MP）、时长（5–30 秒）。
  比例由 App 内固定为 9:16，不再暴露。
### 移除
- **尾帧功能下线**：新版 App 未暴露 `144` 尾帧槽位，脚本不再接受 `last_ref`。
  旧参数名仍可传入，但会被忽略。
### 修复
- `webui.html` 离线兜底表 `EMBEDDED_SCRIPTS` 中的 H3 首帧定义仍是旧版
  （含尾帧、比例、LoRA、steps），后端不可达时会显示出不存在的参数项。
  改为由后端 `REGISTRY` 重新生成，三份副本（根目录 / `android/app/src/main/` / `assets/`）保持一致。
### 清理
- `rh_krea2_nsfw.py` 示例命令中的个性化相册名改为通用占位。
### 已知限制
- `MiniMaxH3ImageToVideo` 官方说明 `trained range is ~124-362`，即 **5–15 秒**。
  实测 20 秒、30 秒可正常出片，但超过 15 秒后部分时段会出现脸部漂移，属超出训练范围的表现，
  非分辨率问题（0.4 MP 下 480×832 与双参考图版完全一致）。
### 版本
- versionCode 27 -> 28，versionName 0.5.14 -> 0.5.15

## [0.5.14] - 2026-10-28
### 修复
- **安卓登录要登两次**：原来只在 `onPageFinished` 里抓一次 token。
  官网是 SPA，登录成功只做客户端路由跳转、不重载页面，抓取窗口早已错过，
  所以第一次登录一定拿不到，要退出重新点一次才行。
  改为弹窗出现即开始轮询（800ms），登录完成与抓取之间不再有时序依赖。
- **抓取改用 Cookie 为主源**：`CookieManager.getCookie()` 同步返回、不依赖 JS 执行，
  且 `Rh-Identify` 只在 Cookie 里有（localStorage 常常没有），以前缺这个字段会导致接口调不通。
  localStorage 降为副源，用于补 `Rh-Comfy-Auth` 等字段。
### 新增
- **冷启动自动恢复登录**：App 启动后若自身 WebView 的 Cookie 里已有 token，
  直接回灌后端，不用再点「一键登录」。
- 登录 WebView 显式开启 Cookie + 第三方 Cookie（Google OAuth 依赖第三方 cookie）。
- 抓到 token 后主动 `CookieManager.flush()` 落盘，避免进程被杀后丢失。
### 版本
- versionCode 26 -> 27，versionName 0.5.13 -> 0.5.14

## [0.5.13] - 2026-10-28
### 变更
- **登录改为「一键登录」**：门禁页只留一个按钮，自动获取登录态，不再需要手动复制粘贴。
  - 网页版：后端直接读本机浏览器的 localStorage（leveldb），读到就写入会话。
  - 安卓版：App 内嵌 WebView 打开官网，登录后自动抓取并导入。
- 账号密码登录降为次要入口（默认折叠），官网 Google 注册的账号没有密码时走一键登录。
### 修复
- **一键登录解析报错**：`o.Rh-Accesstoken` 中键名带连字符，被 JS 解析为减法表达式并抛 ReferenceError，被 catch 静默吞掉，导致整段 localStorage JSON 导入永远失败。改用 `o['Rh-Accesstoken']` 取值。
- 安卓自动抓取失效：官网是 SPA，登录后不重载页面，`onPageFinished` 只触发一次。改为弹窗存活期每 800ms 轮询 localStorage。
- Google OAuth 被拒：去掉 WebView UA 里的 `; wv` 与 `Version/4.0` 标记。
### 新增
- `scripts/rh_browser_capture.py`：只读解析 Chrome/Chromium/Edge/Brave/Vivaldi 的 localStorage leveldb，提取 RunningHub 会话。
- `POST /api/session/capture`：网页版一键登录的后端接口。
### 版本
- versionCode 25 -> 26，versionName 0.5.12 -> 0.5.13

## [0.5.11] - 2026-10-28
### 变更
- **登录页简化**：去掉「🌐 在浏览器中登录（自动导入）」按钮，改为一个「打开官网登录 / 注册 →」链接。
- 安卓端点击该链接会在 App 内打开 RunningHub 官网，用户正常登录后**自动抓取 localStorage 并导入**，无需手动点确认。
- 浏览器环境下该链接为普通外链，提示用户登录后回来用账号密码登录。
### 修复
- 内嵌登录 WebView 原打开 `/Login/login`（SPA 路由，直接打开是空白页）改为打开主页 `https://www.runninghub.ai`。
- token 空值判断失效：原用 `json.contains("token")`，JSON 键名恒存在导致永远为真。改为解析 JSON 后检查值。
- versionCode 23 -> 24，versionName 0.5.11

## [0.5.10] - 2026-10-28
### 修复
- **浏览器自动登录失效**：`/api/session/import` 未加入未登录白名单，导入请求被 401 拦截，前端显示“导入失败/未登录”。已加入白名单。
- **注册链接 404**：原 `Login/EmailRegisterV3` 路径无效，改为 RunningHub 主页 `https://www.runninghub.ai`。
- **上传凭证兜底**：浏览器导入时若 `Rh-Comfy-Auth` 缺失，自动调 `/api/instance/access/auth` 重签。
### 变更
- 删除所有面向用户的「运行次数 / 作者收益 / 通道计数」说明文案（登录门禁、设置面板、状态栏）。

## [0.5.9] - 2026-10-28
### 头条
- **接入网页会话通道**：可直接用 RunningHub 账号登录，通过网页端接口提交任务。
### 新增
- **`scripts/rh_session_channel.py`**（约 590 行）：session 通道完整封装。
  - `login` / `refresh_token` / `_sign_comfy_auth` / `ensure_fresh` / `logout` / `parse_jwt`
  - openapi → session 翻译层：`create_task` / `query_outputs` / `upload_image`
  - 自动续期：每次请求前检查，token 距过期 < 2 分钟自动 refresh；comfy 凭证缺失或将过期自动重签
- **登录入口（强制门禁）**：未登录时全屏遮罩拦截（`z-index 900/901`），脚本列表根本不加载。登录框含账号/密码、注册链接 `https://www.runninghub.ai/Login/EmailRegisterV3`。
- **安卓浏览器自动登录**：App 内弹 WebView 打开 RunningHub 登录页，登录后自动提取 `localStorage` 三件套回灌。新增 `/api/session/import` 接口 + Kotlin `Bridge` 类（`window.AndroidBridge.startBrowserLogin`）。
- **后端未登录守卫**：业务接口一律返回 `401 {"need_login": true}`，白名单：`login/logout/session/session\_refresh/session\_import/settings/backend/update\_check/scripts/latest\_image`。
- **统一弹窗 `uiConfirm` / `uiAlert`**：替代原生 `confirm` / `alert`，样式对齐设置弹窗。
### 变更
- **`scripts/rh_webui.py`**：新增 `_install_session_channel()`，不改 11 个业务模块，只替换底层 `_post` / `_request` / `_req`，把 openapi 请求翻译成 session 请求（幂等）。
- 新增路由：`/api/login`、`/api/logout`、`/api/session/refresh`、`/api/session`（GET/POST）、`/api/session/import`。
- 退出登录后立即弹门禁，不再需要刷新页面。
- 密码框默认明文（`type="text"`），提供 🙈 遮挡开关。
- 删除「高级：手动粘贴令牌」前端入口。
### 修复
- `/api/output/taskHistory` 查询：服务端**忽略 taskId 参数**，改为拉列表后本地过滤。
- `workflows/session.json` 加入 `.gitignore`（含真实 token，不得入库）。

## [0.5.6] - 2026-10-27
### 头条
- **H3 双参考图视频上线**：`h3_omni_video` 从「9 图全能版」切换为「双参考图版」，走已发布 App 通道（`2114838999247724546`），不再走 `/proxy` 自己拼 prompt。
  - 输入 2 张参考图：`ref_image_0` → Picture 1、`ref_image_1` → Picture 2，节点结构由 App 端锁定
  - 时长参数以**帧数**暴露（`[192] PrimitiveInt.value`），换算：124=5 秒 / 243=10 秒 / 362=15 秒（24fps，17n+5 对齐）
  - 视频带人声，音频链路经 `VAELoader(minimax_h3_audio_vae_fp32)` → `MiniMaxH3AVDecodeT8` → `VHS_VideoCombine`

### 新增
- **`scripts/rh_h3_dual_app.py`**：App 通道调用模块（约 530 行）。
  - `SECONDS_TO_FRAMES` 换算表：前端填秒、模块内部换算成帧，解决 App 端只暴露帧数的问题
  - 参考图传本地路径时自动走 `/proxy/{key}/upload/image` 上传
  - 字段白名单从 `/api/webapp/detail` 动态拉取，不硬编码
- **`scripts/rh_moss_app.py`**：安卓端此前缺失，本轮补齐。

### 变更
- **`rh_wan22.py` 网页端与安卓端统一**：此前两端实现分叉（网页端走 App 通道、安卓端走平台托管 `getJsonApiFormat`），现以网页端为准整体覆盖。注意网页端 `run()` 不接受 `width/height/compare/seed/steps`，调用点已同步调整为不传这些参数。
- **`rh_webui.py` 两端对齐**：网页端多参考图逻辑（`ref_image_1`~`ref_image_9`）随 `h3_omni_video` 语义变更一并清理。

### 移除
- **`h3_omni_video` 的 LoRA 开关**：LoRA 已内置并强制开启，运行时 LoRA 选择与强度两个参数从 `h3_omni_video` 面板移除。
  - `h3_flf_video` 的 `lora` / `lora_strength` 参数**仍然保留**：它走 `rh_h3_flf_api`，是另一条工作流，LoRA 由 `apply_lora()` 在运行时注入而非内置。要一并移除需先确认那条工作流是否内置 LoRA。

### 技术
- 三份 `webui.html`、两份 `rh_webui.py`、`rh_h3_dual_app.py`、`rh_moss_app.py`、`rh_wan22.py` 已全部 `cmp` 校验一致（无差异输出）。
- 所有 Python 文件通过 `ast.parse` 语法校验；`REGISTRY` 求值验证两端 key 完全相同（8 项，含 `moss_t2i`）。
- `versionCode` 20 → 21。

### 未验证
- **未编译 APK**，本轮仅改动源码与 assets。
- **未端到端实跑**：web 版与安卓版均未实际提交过任务。
- 成片效果（东北话/台湾腔还原、胸像构图是否守住）未下载检查。
- 历史遗留：`rh_workflow_api.py` 已无入口引用，成为孤儿模块，暂保留未清理。

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
