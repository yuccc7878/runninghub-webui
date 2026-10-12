# RunningHub WebUI

RunningHub 本地统一跑图台：一个自托管的 Web UI，直连 [RunningHub](https://www.runninghub.ai) API 完成文生图 / 图生图 / 视频生成，任务异步化，出图直显。纯 Python 标准库服务端 + 原生 HTML/JS 前端，零第三方依赖。

## 功能特性

- **5 大生成脚本**：Zimage 图生图/文生图、Krea2 文生图、Krea2 图生图、MiniMaxH3 首尾帧视频、MiniMaxH3 全能视频（文生视频/参考图驱动）
- **任务异步队列**：提交后后台轮询 RunningHub，历史任务可重跑 / 删除
- **AI 润色**：接入任意 OpenAI 兼容接口，按 t2i / i2i / video 三套分镜规范自动扩写提示词
- **素材与产物管理**：参考图素材库、生成产物列表、直链预览
- **密钥动态注入**：API Key 通过 WebUI 设置保存到本地，运行时装进各底层模块，代码零硬编码

## 快速开始

```bash
# 1. 启动服务（默认端口 8765）
python3 scripts/rh_webui.py --port 8765

# 2. 浏览器打开
http://localhost:8765
```

首次使用点右上角「⚙ 密钥」配置：

| 配置项 | 说明 |
|---|---|
| RunningHub API Key | 在 runninghub.ai 账号页获取，保存后所有脚本生效 |
| LLM Base URL / Model / Key（可选） | 任意 OpenAI 兼容接口，用于 AI 润色提示词 |

## 目录结构

```
├── scripts/               # 服务端与各生成脚本模块
│   ├── rh_webui.py        # WebUI 服务端（入口）
│   ├── rh_zimage_api.py   # Zimage 工作流
│   ├── rh_krea2_nsfw.py   # Krea2 文生图/图生图
│   ├── rh_h3_flf_api.py   # MiniMaxH3 首尾帧视频
│   ├── rh_workflow_api.py # MiniMaxH3 全能视频
│   └── rh_h3_lora.py      # LoRA 预设表（纯本地）
├── webui.html            # 前端页面（单文件）
├── refs/                  # 参考图素材库（运行时生成）
└── workflows/             # 运行时状态、配置与产物（不入库）
```

## 生成脚本一览

| 脚本 | 模式 | 说明 |
|---|---|---|
| Zimage 图生图/文生图 | I2I + T2I | 选参考图=图生图，纯提示词=文生图 |
| Krea2 文生图 | T2I | Krea2 底模，支持权重语法 `(词:1.5)` |
| Krea2 图生图 | I2I | 参考图注入保持角色，改姿势/背景 |
| MiniMaxH3 首尾帧视频 | I2V | 首帧(→尾帧)生成视频，支持续接 |
| MiniMaxH3 全能视频 | T2V / omni | 文生视频 / 参考图驱动 |

## AI 润色规范

润色接口按脚本类型分三套 system prompt（见 `rh_webui.py` 的 `_POLISH_PROMPTS`）：

- **t2i**：七段固定结构（外貌锚点 → 场景 → 光线 → 动作 → 服装 → 镜头 → 氛围），外貌缺失自动补默认人物锚点
- **i2i**：只描述修改目标，姿势跟随原图，不引入身份改变元素，短句克制
- **video**：分镜表格式（`时间段｜画面｜动作｜台词｜音轨`），动作写已完成状态，台词软糯 25 字内

## 安全说明

- **代码零硬编码密钥**：所有 API Key 均为运行时注入，保存在本地 `workflows/api_key.json`（已 gitignore）
- 本仓库只包含代码，不含任何生成产物、素材、状态数据
- 请自行保管好 RunningHub / LLM 密钥，勿提交到公开仓库

## 免责声明

本项目用于个人学习与自用研究。请遵守 RunningHub 平台用户协议，合理使用 API 额度。
