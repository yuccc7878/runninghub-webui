# 图生图 App 发布说明书

> 对象：工作流 `2112633650515902466`（图生图基础款）
> App：`2112645746440085506`（**已发布，暴露 10 个字段，含 LoRA**）
> 本文所有参数取自**实测跑通并出图**的那一版，非推测值

---

## 0. 当前状态（2026-10-21 05:23 已更新，不再是「不一致」）

服务器 V2 版本内容 = 跑成功的那一版，且 LoRA 已串接并暴露。

| 项 | 值 | 状态 |
|---|---|---|
| 节点数 | 16 | ✅ |
| 提交图节点 | 15（含两个 LoRA） | ✅ |
| `KSampler.latent_image` | `["32",0]` VAEEncode | ✅ |
| `denoise` | **0.65** | ✅ |
| `Mask_ClothesSegment` / `GrowMask` 等 inpaint 节点 | 已删除 | ✅ |
| LoRA `14` / `20` mode | `0`（启用，非 bypass） | ✅ |
| 暴露字段 | **10 个**（6 基础 + 4 LoRA） | ✅ |
| V2 版本更新时间 | `2026-10-21 05:23:28` | ✅ |
| App `publishTime` | `2026-10-21 04:42:52`（首次，未变） | ✅ |

### 0.1 保存通道失效与破法（关键经验）

这个现象反复踩，必须记清：

| 触发动作 | 实际行为 |
|---|---|
| 点「保 存」按钮 | **常常什么请求都不发**（React handler 拿不到），画布改动只留内存 |
| 点「运行」→ `/api/workflow/setContent` 返回 `code:0` | 写入的是固定 `V1`（`versionId: 2112633729633296386`），**不覆盖画布内容** |
| **用 `?versionId=<V2的id>` 打开画布**，再点「发布」 | ✅ `setContent` 返回 `versionId: 2112650976242245634 / V2`，**真正写进去** |

**结论**：要改服务器内容，唯一可靠路径是
1. 从发布页点「编辑工作流」进入（URL 自带 `?versionId=...`）
2. 在画布上改完
3. 点「发布」→ 弹「审核通过，是否继续发布？」→ 再点「发 布」

> ⚠️ 不要在错误配置下点「发布」——发布本身会先强制保存当前画布。

> ⚠️ 点「发布」前若控制台出现 `window.open blocked due to active file chooser`，
> 说明有挂起的文件选择器挡住了后续请求，必须先 `cancel` 掉文件选择器。

---

## 1. 目标结构（16 节点）

### 1.1 主链路

```
30 LoadImage
   └→ 31 ImageScale (lanczos, crop=disabled)
        └→ 32 VAEEncode ← 3 VAELoader
             └→ 4 KSampler.latent_image
                  ├→ 1 UNETLoader (z_image_turbo_bf16)
                  ├→ 5 CLIPTextEncode (正向) ← 22 CLIPLoader
                  ├→ 6 ConditioningZeroOut (负向)
                  └→ 8 VAEDecode ← 3 VAELoader
                       └→ 10 SaveImage
```

### 1.2 提交图实测结果（15 节点，含两个 LoRA）

| id | class_type | 关键参数 |
|---|---|---|
| 1 | UNETLoader | `z_image_turbo_bf16.safetensors` / `default` |
| 3 | VAELoader | `z_image_turbo-vae.safetensors` |
| 4 | KSampler | steps=6, cfg=1, denoise=**0.65**, `res_2s` / `bong_tangent` |
| 5 | CLIPTextEncode | 提示词（暴露给用户） |
| 6 | ConditioningZeroOut | 负向（cfg=1 下无效，但保留） |
| 8 | VAEDecode | ← 4, ← 3 |
| 10 | SaveImage | ← 8 |
| 16 | Int | **1080**（宽度） |
| 17 | Int | **1920**（高度） |
| 22 | CLIPLoader | `qwen_3_4b.safetensors` / `lumina2` / `default` |
| 30 | LoadImage | 参考图（暴露给用户） |
| 31 | ImageScale | `lanczos`, `crop=disabled`, w←16, h←17 |
| 32 | VAEEncode | ← 31, ← 3 |

### 1.3 LoRA 链路（已启用，已进提交图）

从 2026-10-21 05:23 那次发布起，两个 LoRA **不再 bypass**，已串进 model 链：

```
1 UNETLoader (z_image_turbo_bf16)
   └→ model
      20 LoraLoaderModelOnly  Kook_Zimage_如梦似幻 @0.25
         └→ model
            14 LoraLoaderModelOnly  Kook_Zimage_瑶光 @0.3
               └→ model
                  4 KSampler.model
```

| id | 类型 | mode | 文件 | 强度 | 进提交图 |
|---|---|---|---|---|---|
| 14 | LoraLoaderModelOnly | **`0`** | `Kook_Zimage_瑶光.safetensors` | 0.3 | ✅ |
| 20 | LoraLoaderModelOnly | **`0`** | `Kook_Zimage_如梦似幻.safetensors` | 0.25 | ✅ |

> 顺序说明：提交图里 `4.model = ["20",0]`，`20.model = ["14",0]`，`14.model = ["1",0]`。
> 即 UNET → 14 瑶光 → 20 如梦似幻 → KSampler。链的方向和 ID 大小相反，别接错。

### 1.4 不进提交图的节点（正常，别误删）

| id | 类型 | mode | 说明 |
|---|---|---|---|
| 23 | Note | `0` | 分辨率说明文字，Note 类型天然不入图 |

---

## 1.5 LoRA 运行时替换（核心能力，已实测）

### 1.5.1 原理

发布时把 `14` / `20` 的 `lora_name` 和 `strength_model` 都暴露成 App 输入参数，
运行时通过 `nodeInfoList` 覆盖，就能**任意换 LoRA，不用改工作流、不用重新发布**。

### 1.5.2 App 暴露的 10 个字段

| nodeId | 节点 | 字段 | 类型 | 发布时默认值 |
|---|---|---|---|---|
| 4 | KSampler | `steps` | INT | 6 |
| 4 | KSampler | `denoise` | FLOAT | 0.65 |
| 5 | CLIPTextEncode | `text` | STRING | 换装提示词 |
| 16 | Int | `value` | INT | 1080 |
| 17 | Int | `value` | INT | 1920 |
| 30 | LoadImage | `image` | IMAGE | api/xxx.png |
| **20** | **LoraLoaderModelOnly** | **`lora_name`** | **LORA** | `Kook_Zimage_如梦似幻.safetensors` |
| **20** | **LoraLoaderModelOnly** | **`strength_model`** | **FLOAT** | 0.25 |
| **14** | **LoraLoaderModelOnly** | **`lora_name`** | **LORA** | `Kook_Zimage_瑶光.safetensors` |
| **14** | **LoraLoaderModelOnly** | **`strength_model`** | **FLOAT** | 0.3 |

### 1.5.3 调用示例

```json
{
  "webappId": "2112645746440085506",
  "apiKey": "<API_KEY>",
  "nodeInfoList": [
    {"nodeId": "5",  "fieldName": "text",           "fieldValue": "换装提示词"},
    {"nodeId": "30", "fieldName": "image",          "fieldValue": "api/xxx.png"},
    {"nodeId": "4",  "fieldName": "denoise",        "fieldValue": "0.65"},
    {"nodeId": "14", "fieldName": "lora_name",      "fieldValue": "无滤真实_V2.safetensors"},
    {"nodeId": "14", "fieldName": "strength_model", "fieldValue": "0.7"},
    {"nodeId": "20", "fieldName": "lora_name",      "fieldValue": "Kook_Zimage_如梦似幻.safetensors"},
    {"nodeId": "20", "fieldName": "strength_model", "fieldValue": "0.25"}
  ]
}
```

### 1.5.4 三个必须知道的坑

**坑 1：`lora_name` 必须传真实文件名，且必须在平台候选列表里**

```
空串 ""        → 433 value_not_in_list: lora_name: '' not in (list of length 75293)
字符串 "None"  → 同样被拒
不存在的文件名 → 同样被拒（例如 charm.safetensors，那只是别名不是真名）
```

报错里那个 **75293** 说明后端候选列表是完整的 7 万多个文件——
只是发布页 `fieldData` 返回 `[[]]` 让前端下拉框显示为空，**API 提交完全不受影响**。

平台真实文件名（`fieldData=[[]]` 看不到，只能靠 `rh_zimage_v2.KNOWN_LORAS` 或实测拿）：

| 用途 | 真实文件名 | 推荐强度 |
|---|---|---|
| 人像高细节 | `zimage-turbo_Cc-风情万种-人像.safetensors` | 0.8~1.2 |
| 去 AI 味 | `无滤真实_V2.safetensors` | 0.5~0.9 |
| 真实感光影 | `ZIT-flow-dpo-lora.safetensors` | 0.6~1.0 |
| 全能模特 | `zimage_全能模特.safetensors` | 0.45 |
| 蜜桃臀 | `🍑ins-V2-Zimage.safetensors` | 0.9 |
| 二次元 | `Z-Anime04.safetensors` | 0.7~1.0 |

**坑 2：不能靠传空值「关掉」某个 LoRA**

因为字段一旦提交就必须是合法文件名。想关掉只能：

| 方案 | 做法 | 代价 |
|---|---|---|
| a）强度设 0 | `strength_model=0` | LoRA 仍加载占显存，效果约等于不生效 |
| b）换中性 LoRA | 换一个影响小的 | 需要找一个中性的 |
| c）改工作流 bypass | 画布把该节点改 `mode=4` 后重新发布 | 要发新版本，且以后就不能换这个位子了 |

**坑 3：没提交的字段 = 沿用发布默认值，不会报错**

这是便利也是陷阱：只提交 `14.lora_name` 不提交 `20.lora_name`，
结果是 `14` 换成新的、`20` 仍是发布时的 `Kook_Zimage_如梦似幻 @0.25`。
所以「只换一个位子」时要清楚另一个位子还在生效。

### 1.5.5 实测记录

| 测试 | lora_name | 结果 |
|---|---|---|
| 默认（不传 LoRA 字段） | — | ✅ `2112656921029050369` |
| 两个位子都传默认名 | 瑶光 / 如梦似幻 | ✅ `2112656405983973377` |
| 只传 14 | 瑶光 @0.9 | ✅ `2112656663702982658` |
| 换 `风情万种` | `zimage-turbo_Cc-风情万种-人像.safetensors` @0.8 | ✅ `2112657342812712962` |
| 换 `无滤真实` | `无滤真实_V2.safetensors` @0.8 | ✅ `2112657468046241794` |
| 换 `flow-dpo` | `ZIT-flow-dpo-lora.safetensors` @0.8 | ✅ `2112657593761857538` |
| 组合（A=风情万种@1.0 + B=如梦@0.25） | — | ✅ `2112657768513339393` 出图 1080×1920 |
| 空串 / "None" / 别名 `charm` 直接当文件名 | — | ❌ `433 value_not_in_list` |

---

## 2. 关键参数说明

### 2.1 denoise = 0.65（核心）

这是决定「改多少」的唯一旋钮。

```
起始 timestep = denoise × 总步数
```

| denoise | 行为 | 适用 |
|---|---|---|
| 0.1~0.3 | 几乎不动，只微调质感 | 磨皮、调色 |
| 0.4~0.5 | 保留构图，换衣服/换发型 | 轻度改 |
| **0.65** | **保留人物骨架与脸型，服装与局部质感重绘** | **本 App 默认** |
| 0.75~0.85 | 结构开始漂移，脸会变 | 大改风格 |
| 1.0 | 完全忽略参考图，等同文生图 | 不用 |

实测 `0.65` 下：人物身份、脸型、发型、姿态、背景、构图全部保留，衣服被替换。这是本 App 的产品定位——**在保留角色的前提下重绘**。

### 2.2 cfg = 1 + steps = 6

Z-Image-Turbo 是蒸馏模型：

- `cfg=1` 时**负面提示词完全无效**（数学上被消掉），所以 `6 ConditioningZeroOut` 只是占位
- `cfg` 提到 2.5 以上画面容易崩坏，不建议作为默认
- `steps=6` 是蒸馏推荐档；提到 8~10 细节会多一点，耗时线性上升

### 2.3 分辨率联动

`16 Int`（宽）和 `17 Int`（高）通过 `ImageScale` 驱动输出尺寸。

- 当前 **1080×1920（9:16 竖屏）**
- 若参考图是横图或方图，输出会被**拉伸变形**（`crop=disabled`）
- 发布时把 16 / 17 暴露为可调参数，让 App 用户按自己图的比例选

---

## 3. 参考图上传（最容易踩的坑）

### 3.1 问题

用官方 API `/task/openapi/upload` 上传后，返回：

```json
{"code":0,"msg":"success","data":{"fileName":"api/dada1075644204ce4026974e9ec2d10f3582b4785a03f3473beb84e7d88a17e0.png","fileType":"image"}}
```

把这个 `fileName` 写进 `30 LoadImage.image`，**画布下拉列表里看不到它**（下拉只有 `example.png` / `None` / `keep_this_dic`），因为 UI 列表来自交互式 input 目录，API 上传落在另一个空间。

### 3.2 结论：不用管下拉列表，直接写值

后端运行时**能解析 `api/xxx.png` 这个路径**。实测：写进去 → 运行 → 出图成功。

```js
// 画布内执行
const w = document.querySelector('iframe[title="comfyUI"]').contentWindow;
const g = w.app.graph;
const li = g.getNodeById(30);
const wd = li.widgets.find(x => x.name === 'image');
wd.value = 'api/dada1075644204ce4026974e9ec2d10f3582b4785a03f3473beb84e7d88a17e0.png';
if (li.widgets_values) li.widgets_values[0] = wd.value;
g.change(); g.setDirtyCanvas(true, true);
```

### 3.3 失败的排查线索（供复用）

之前跑一次 16 秒必失败，换成 `example.png` 后不再卡 16 秒——证明 **16 秒固定耗时是前置校验被拒**，不是模型加载慢。模型加载慢会长于 16 秒，且耗时不稳定。

| 耗时 | 含义 |
|---|---|
| 3 秒失败 | 提交阶段就被拒（参数/结构问题） |
| 16 秒失败 | 输入解析阶段失败（文件找不到） |
| 正常 | 51 秒（Standard, 1080×1920, steps=6） |

---

## 4. 保存与运行

### 4.1 保存

- 本画布是 **手动保存（Alt+S）** 模式
- **JS 触发保存按钮无效**——实测：JS 点保存 → 提示「工作流已保存」→ 重载后改动丢失；用户手动点 → 重载后保留
- 每轮改动都要**手动保存一次**，再做重载验证

### 4.2 运行

- 按钮文字：`运行 Lite/Standard`（用这个，最省）
- 其他：`运行 Lite/Plus`、`运行 Ultra·6000D`
- 点错 Plus 会按 Plus 计费（之前误点过一次，49 秒）

### 4.3 保存后必须做重载验证

```js
// 重载后执行，确认落库
const w = document.querySelector('iframe[title="comfyUI"]').contentWindow;
const pr = await w.app.graphToPrompt();
const chk = id => pr.output[String(id)]?.inputs;
({
  nodes: w.app.graph._nodes.length,
  denoise: chk(4).denoise,
  latent: chk(4).latent_image,
  image: chk(30).image,
  res: { w: chk(16).value, h: chk(17).value },
});
```

合格标准：

```json
{"nodes":16,"denoise":0.65,"latent":["32",0],"res":{"w":1080,"h":1920}}
```

`latent` 必须是 `["32",0]`。若是 `["34",0]`，说明还在 21 节点失败版上。

---

## 5. 发布流程

### 5.1 前置条件

- [ ] 已跑通一次并出图（平台强制要求，未跑通不给发布）
- [ ] 服务器版本已重载验证 = 16 节点 / denoise 0.65
- [ ] 手动保存过一次

### 5.2 点击发布

按钮文字含全角空格：`发 布`。匹配时先去掉所有空白：

```js
const b = [...document.querySelectorAll('button')]
  .find(x => (x.innerText || '').replace(/\s/g, '') === '发布');
b?.click();
```

弹确认框「审核通过，是否继续发布？」，点 `发 布`（同样去空白匹配）→ 跳转 `/publish/2112633650515902466`。

> ⚠️ 这一步会**先把当前画布写进服务器**，所以草稿必须已经是正确版本。

### 5.3 五个步骤

| 步骤 | 内容 | 图生图要点 |
|---|---|---|
| 1 | 版本与工作流 | 应用名、版本号、封面（中文 + 海外各一张）、标签、介绍、输入建议 |
| 2 | 添加输入节点 | 勾选要暴露的参数，见 5.4 |
| 3 | 输出 | 选 `10 SaveImage` |
| 4 | 定价 | 按平台默认或自定 |
| 5 | 审核 | 提交 |

> 封面上传在 WebView 下会失败，需走 COS 直传（见 `runninghub-workflow-ops` 第 5.2 节）。

### 5.4 暴露原则

**只暴露内容类，模型与调度锁死。**

| 暴露 | 节点 | 参数 | 理由 |
|---|---|---|---|
| ✅ | `5 CLIPTextEncode` | `text` | 核心输入 |
| ✅ | `30 LoadImage` | `image` | 核心输入（参考图） |
| ✅ | `16 Int` | `value` | 宽度 |
| ✅ | `17 Int` | `value` | 高度 |
| ✅ | `4 KSampler` | `denoise` | 进阶可调，给 0.45~0.85 建议 |
| ❌ | `1 UNETLoader` | — | 模型锁死 |
| ❌ | `3 VAELoader` | — | 模型锁死 |
| ❌ | `22 CLIPLoader` | — | 模型锁死 |
| ✅ | **`20` / `14` LoRA** | **`lora_name` + `strength_model`** | **已暴露（4 个字段），用于运行时换 LoRA** |
| ❌ | `4 KSampler` | `steps` / `cfg` | 蒸馏模型固定 6 / 1 |
| ❌ | `4 KSampler` | `seed` | 随机 |
| ❌ | `31 ImageScale` | — | `lanczos` 固定 |

> 说明：上表第 5 行的 `4 KSampler steps` 实际是**暴露了**的（App 有 `4.steps`），
> 这里标 ❌ 是指不建议用户改——蒸馏模型固定 6 最稳。

### 5.5 提示词默认值

发布页的输入建议填这个，作为 App 打开时的示例：

```
将画面中人物的衣物更换为简约比基尼，保持人物身份、脸型、发型、姿态、背景与构图不变，
皮肤质感真实，自然光影一致，真实照片质感，高细节，zimage 风格
```

---

## 6. 发布后自检

- [ ] App 详情页能打开
- [ ] 上传自己的参考图能识别
- [ ] 输出尺寸 = 设置的 16 × 17
- [ ] 换装效果确实生效（不是原图直出，也不是完全重绘）
- [ ] Standard 模式耗时约 50 秒（明显超出说明有节点异常）

---

## 7. 已弃用方案（不要回头）

### 7.1 inpaint 局部修改（`Mask_ClothesSegment` 链路）

链路：`30 → 33 Mask_ClothesSegment → 35 GrowMask → 34 SetLatentNoiseMask → 4.latent_image`

- 节点能保存、能进提交图，但**运行失败**（16 秒）
- 怀疑原因：`ComfyUI-Apt_Preset` 的人体解析模型需运行时下载，16 秒固定失败不符合下载超时特征，更像输入校验被拒
- 放弃理由：`denoise=0.65` 全图重绘已经达成「换衣服」目标，inpaint 的额外收益不足以抵偿调参成本（mask 边界接缝、多部位组合、模型可用性不确定）

### 7.2 switch 节点做部位切换

- `ComfySwitchNode` / `1hew_AnySwitchSelect` 能进 `graphToPrompt()`，但**保存后重载丢失**
- 平台在保存时丢弃带 `lazy` / 通配类型的节点

### 7.3 旧画布 `2112321420339535874`

- 「实时保存」模式下保存通道失效，所有修改都写不进服务器
- 已改为文生图结构，不再使用

---

## 8. 相关文件

| 用途 | 路径 |
|---|---|
| 画布配方与踩坑 | `skills/runninghub-canvas-build/SKILL.md` |
| 改参数 / 发布 / API 直调 | `skills/runninghub-workflow-ops/SKILL.md` |
| App 通道调用脚本 | `scripts/rh_zimage_app.py` |
| WebUI 注册表 | `scripts/rh_webui.py`（`REGISTRY["zimage_i2i"]`） |
| WebUI 界面 | `webui.html`（`EMBEDDED_SCRIPTS` + `lora_name` 控件） |
| 文生图 App 信息（可对照） | `RUNNINGHUB_APP.md` |
| API Key | `workflows/api_key.json` |

---

## 9. 实测记录

### 9.1 基础换装（16 节点全图重绘版）

| 项 | 值 |
|---|---|
| taskid | `2112643959189721089` |
| 模式 | Standard |
| 耗时 | 00:51 |
| 分辨率 | 1080×1920 |
| 参考图 | 洛宝日常照（`api/dada1075644204ce4026974e9ec2d10f3582b4785a03f3473beb84e7d88a17e0.png`） |
| 参数 | denoise=0.65, cfg=1, steps=6, res_2s, bong_tangent, lanczos |
| 结果 | 成功，服装替换为比基尼，人物一致性保留 |

### 9.2 LoRA 运行时替换（位置 A 换 `风情万种`）

| 项 | 值 |
|---|---|
| taskid | `2112657768513339393` |
| 提交字段 | 10 个（含 4 个 LoRA 字段） |
| LoRA 位A | `zimage-turbo_Cc-风情万种-人像.safetensors` @1.0 |
| LoRA 位B | `Kook_Zimage_如梦似幻.safetensors` @0.25 |
| 分辨率 | 1080×1920 RGB，2583 KB |
| 产物 | `洛宝_图生图_LoRA替换_风情万种_1080x1920.png` |
| 结果 | **成功**，证明 lora_name 运行时替换真实生效 |

### 9.3 WebUI 端到端（短名 `nofilter`，不勾位B）

| 项 | 值 |
|---|---|
| WebUI 任务 | `T1070_1792532030` |
| 远端 taskid | `2112658583735042050` |
| 提交参数 | `lora_a_name=nofilter`, `lora_a_strength=0.7`, `lora_b_name=`（空） |
| 实际提交字段 | 8 个（位B 未勾 → 不提交，沿用发布默认 `如梦似幻 @0.25`） |
| 产物 | `洛宝_图生图_webui_LoRA短名_1080x1920.png`（1080×1920 RGB，1789 KB） |
| 结果 | **成功**，短名 → 真实文件名 → 后端接受，全链路通 |

---

## 10. WebUI 使用说明（zimage_i2i）

### 10.1 参数面板

| 控件 | 类型 | 说明 |
|---|---|---|
| 参考图 | 槽位 | 不选 = 走文生图 App |
| 分辨率 | 下拉 | 1080×1920 推荐 |
| 步数 | 数字 | 6（蒸馏模型） |
| 重绘强度 | 数字 | 0.65（换装保留人物） |
| **LoRA 位A · 瑶光位** | 勾选 + 强度 + **文件名框** | 留空 = 沿用 `Kook_Zimage_瑶光 @0.3` |
| **LoRA 位B · 如梦位** | 勾选 + 强度 + **文件名框** | 留空 = 沿用 `Kook_Zimage_如梦似幻 @0.25` |

### 10.2 提交规则

| 状态 | 提交的字段 | 效果 |
|---|---|---|
| 不勾 | 不提交该位的 `lora_name` / `strength_model` | 沿用发布默认 LoRA |
| 勾选但文件名留空 | 同样不提交（前端与后端双重兜底） | 沿用发布默认 LoRA |
| 勾选 + 填短名（如 `charm`） | 脚本展开成真实文件名 | ✅ 推荐用法 |
| 勾选 + 填真实文件名 | 原样提交 | ✅ 也可 |

> 空串不会被提交：`webui.html` 的 `lora_name` 控件和 `rh_zimage_app.build_node_info`
> 都做了 `val === ""` 过滤，避免触发后端 `433 value_not_in_list`。

### 10.3 可用短名

```
charm    → zimage-turbo_Cc-风情万种-人像.safetensors   @1.0
nofilter → 无滤真实_V2.safetensors                      @0.7
flowdpo  → ZIT-flow-dpo-lora.safetensors                @0.8
model    → zimage_全能模特.safetensors                  @0.45
peach    → 🍑ins-V2-Zimage.safetensors                  @0.9
zanime   → Z-Anime04.safetensors                        @1.0
```

代码里 `python3 -c "import rh_zimage_app as A; print(A.list_loras())"` 可打印这张表。
