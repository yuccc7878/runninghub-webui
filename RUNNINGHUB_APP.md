# RunningHub App 信息

## 应用地址
- App ID: 2111898663873490946
- 移动端: https://www.runninghub.ai/zh-cn/ai-detail-mobile/2111898663873490946
- 桌面端: https://www.runninghub.ai/zh-cn/ai-detail/2111898663873490946
- 工作流: https://www.runninghub.ai/zh-cn/workflow/2111844121152663553

## 名称
zimage低成本极速版

## 工作流
- 自建工作流 ID: 2111844121152663553
- 节点数: 10 (9 基础 + 1 LoRA)

## 暴露给 App 的输入参数 (6 项)
| 中文标签 | 节点 | 参数 | 默认值 |
|---|---|---|---|
| 提示词 | 6 CLIPTextEncode | text | 示例提示词 |
| 宽度 | 5 EmptyLatentImage | width | 1080 |
| 高度 | 5 EmptyLatentImage | height | 1920 |
| 步数 | 3 KSampler | steps | 8 |
| 风格 LoRA | 45 LoraLoaderModelOnly | lora_name | ZIT-flow-dpo-lora.safetensors |
| LoRA 强度 | 45 LoraLoaderModelOnly | strength_model | 0.75 |

## 固定参数 (不暴露)
- KSampler: cfg=1.0, denoise=1.0, sampler_name=res_2s, scheduler=bong_tangent, seed=随机
- EmptyLatentImage: batch_size=1
- UNETLoader: z_image_turbo_bf16.safetensors / default
- CLIPLoader: qwen_3_4b.safetensors / lumina2
- VAELoader: z_image_turbo-vae.safetensors
- CLIPTextEncode[7] 负向提示词: 空 (cfg=1.0 下无效)

## 实测性能
| 分辨率 | 耗时 |
|---|---|
| 1080x1920 | 24-28s |
| 1024x1368 | 28-33s |
| 1152x1536 | 24s |

## 可用 LoRA (6 个，均已实测)
| 预设 | 文件 | 推荐强度 | 作用 | 步数代价 |
|---|---|---|---|---|
| flowdpo (默认) | ZIT-flow-dpo-lora.safetensors | 0.8 | 真实光影，去塑料感 | 无 |
| charm | zimage-turbo_Cc-风情万种-人像.safetensors | 1.0 | 高细节人像，锐度最高 | 无 |
| nofilter | 无滤真实_V2.safetensors | 0.7 | 去 AI 味，素人皮肤纹理 | 12 步 |
| model | zimage_全能模特.safetensors | 0.45 | 全能模特展示 | 10 步 + cfg 1.1 |
| peach | ins-V2-Zimage.safetensors | 0.9 | 蜜桃臀加强 (会改脸型) | 无 |
| zanime | Z-Anime04.safetensors | 1.0 | 二次元动漫化 | 无 |

## 关键平台限制 (踩坑记录)
1. 程序化修改不被保存: LiteGraph.createNode、直接改 widget.value 都不触发服务端草稿保存，必须人工操作。
2. 第三方自定义节点不支持发布: ResolutionSelector、easy loraStack 等即使加进 graph，getJsonApiFormat 也不序列化，发布候选不出现。
3. 只有自建工作流可发布: 魔改他人工作流无法发布成 App。
4. 空值参数会被丢弃: lora_name 为 null 时整个字段不写入服务端，运行时缺参数报错。

## 相关文件
- API 模块: scripts/rh_zimage_v2.py (12 种分辨率预设 + 6 个 LoRA + 叠加 + 自动调参)
- 旧工作流 (他人，保留作回退): 2108658505526910977
