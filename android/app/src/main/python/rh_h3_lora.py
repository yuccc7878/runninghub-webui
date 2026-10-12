#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniMaxH3 LoRA 注入助手（共享模块）
====================================
给 rh_h3_flf_api.py（首尾帧 I2V）与 rh_workflow_api.py（omni 参考）共用。

核心原则（2026-10-11 实测）：
    【新增串联，绝不替换】工作流自带的 turbo 8step LoRA 必须保留。

    turbo LoRA（minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16）是 8 步加速 LoRA，
    与 KSampler 的 steps=8 绑定。摘掉它而 steps 不改 → 严重欠采样 → 画面糊。

    实测对照（256x448 / 5s / 同 seed 20261011，仅差 turbo 一个变量）：
        保留 turbo：清晰度（拉普拉斯方差）527.2，91s / 19 币
        删除 turbo：清晰度 120.4，100s / 20 币
    → 清晰度差 338%，且去掉 turbo 反而更慢更贵。

    所以正确链路是：
        UNETLoader → turbo_LoRA(1.0) → 自定义_LoRA(0.9) → 下游 patch 节点

已知 LoRA（RunningHub 平台 loras 目录）：
    - SynthPussy_MinimaxH3_v1.safetensors
      「MinimaxH3-更好的猫咪」阴部细节加强，基础模型 minimax-h3，
      无触发词，作者推荐强度 0.9。
      作者说明：训练未用视频，但可提示手指抚摸/张开动作；
                假阳具动作仍需额外 LoRA，提示插入物体形变基本正确。
"""

# 各工作流的 turbo LoRA 挂载点：turbo 节点 ID → 下游接收节点 ID
TURBO_ANCHOR = {
    # workflow_id: (turbo_lora_node, downstream_node_that_consumes_model)
    "2108688116525977602": ("156", "157"),   # minimaxH3首尾帧视频工作流（超级加速版）
    "2108502955011149825": ("210", "225"),   # minimaxH3超级加速全能工作流
}

# 已验证可用的自定义 LoRA
KNOWN_LORAS = {
    "synthpussy": {
        "file": "SynthPussy_MinimaxH3_v1.safetensors",
        "default_strength": 0.9,
        "desc": "阴部细节加强（MinimaxH3-更好的猫咪）。无触发词，作者推荐 0.9。"
               "✅ T2V 实测通过（10s/608x1056，303s/61币）",
    },
    # ---- 以下为 2026-10-11 搜集的候选，仅登记未实测 ----
    # 全部串在 turbo 之后（见 TURBO_ANCHOR），绝不替换 turbo
    "realface": {   # Minimax-H3 真实感人物特征
        "file": "minimax-h3-people.safetensors",
        "default_strength": 0.8,
        "desc": "人物真实质感增强。触发词 r34l1sm（需写进提示词）",
    },
    "cinematic": {  # Minimax H3 真实电影质感
        "file": "电影镜头_000019000.safetensors",
        "default_strength": 0.7,
        "desc": "电影级质感，解决塑料感/过度锐化/光线生硬/景深。"
                "触发词 DY，官方强度 0.7（剧烈运动降到 0.5）",
    },
    "physics": {    # minimax-h3-spatial-physics-lora
        "file": "wushu_spatial_physics_clean_3000_pruned.safetensors",
        "default_strength": 0.4,
        "desc": "物体空间关系与物理运动（碰撞/堆叠/掉落/遮挡）。"
                "无触发词，作者建议 0.3-0.5，仍不稳定。⚠️ 只管物体不管人",
    },
    "nokia": {      # Nokia N7610 - MiniMax H3 Style LoRA
        "file": "nokia_style_h3_ep50.safetensors",
        "default_strength": 1.0,
        "desc": "诺基亚手机拍摄风格（复古低清质感）。触发词 nokiastyle，推荐 1.0",
    },
}

# 新增 LoRA 节点的起始 ID（避免与工作流原有节点冲突）
LORA_NODE_ID = "900"


def resolve_lora(lora):
    """把 --lora 参数解析成 (文件名, 强度)。支持预设名或直接文件名。"""
    if not lora:
        return None, None
    if lora in KNOWN_LORAS:
        return KNOWN_LORAS[lora]["file"], KNOWN_LORAS[lora]["default_strength"]
    # 直接传文件名
    return lora, 0.9


def apply_lora(p, lora_name, strength, workflow_id,
               node_id=LORA_NODE_ID, verbose=True):
    """
    在 turbo LoRA 之后串入一个自定义 LoRA。

    参数:
        p:           prompt dict（会被就地修改）
        lora_name:   LoRA 文件名，或 None 表示不挂载
        strength:    LoRA 强度
        workflow_id: 用于查 turbo 挂载点
        node_id:     新节点 ID

    返回:
        (p, 是否挂载)
    """
    if not lora_name:
        return p, False

    if workflow_id not in TURBO_ANCHOR:
        raise RuntimeError(
            f"未知工作流的 turbo 挂载点: {workflow_id}。"
            f"请先确认该工作流的 turbo LoRA 节点 ID 与下游节点，再补进 TURBO_ANCHOR。"
        )

    turbo_id, downstream_id = TURBO_ANCHOR[workflow_id]
    if turbo_id not in p:
        raise RuntimeError(
            f"工作流 {workflow_id} 里找不到 turbo LoRA 节点 [{turbo_id}]，"
            f"链路可能已变，请重新核对后再挂 LoRA。"
        )

    # 串在 turbo 之后：turbo.model 的原来源不动，只把下游改指新节点
    p[node_id] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "lora_name": lora_name,
            "strength_model": strength,
            "model": [turbo_id, 0],
        },
    }
    # 下游原本接 turbo 的，改接新 LoRA
    p[downstream_id]["inputs"]["model"] = [node_id, 0]

    if verbose:
        print(f"[LoRA] {lora_name} @ {strength} → "
              f"[{turbo_id}] turbo → [{node_id}] → [{downstream_id}]")
    return p, True