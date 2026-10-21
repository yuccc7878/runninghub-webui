# -*- coding: utf-8 -*-
"""
rh_zimage_v2.py —— zimage 极速版（自有工作流 2111844121152663553）

8 步极速出图，支持 12 种分辨率预设 + 6 个强化 LoRA（可叠加），
根据所挂 LoRA 自动调整步数/CFG。

与 rh_zimage_api.py 的区别：
  - 旧模块绑定工作流 2108658505526910977（别人的），节点映射写死在 NODE 里
  - 本模块绑定自己的工作流（可发布为 App）
  - 支持分辨率预设、LoRA 叠加（可挂多个）、LoRA 驱动的自动调参

节点结构（2026-10-19 实测）：
  38 UNETLoader          z_image_turbo_bf16.safetensors
  39 CLIPLoader          qwen_3_4b.safetensors (lumina2)
  40 VAELoader           z_image_turbo-vae.safetensors
   6 CLIPTextEncode      正向 ← clip 39
   7 CLIPTextEncode      负向（空）← clip 39
   5 EmptyLatentImage    宽高直填（无 Int 控制器）
   3 KSampler            res_2s / bong_tangent / steps 8 / cfg 1.0
  37 VAEDecode
   9 SaveImage
"""
import sys
import os
import json
import random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rh_zimage_api as Z

# ==================== 配置 ====================
WORKFLOW_ID = "2111844121152663553"

NODE = {
    "unet":        "38",
    "clip":        "39",
    "vae":         "40",
    "pos":         "6",
    "neg":         "7",
    "empty":       "5",
    "ksampler":    "3",
    "vae_decode":  "37",
    "save":        "9",
}

# LoRA 节点从 900 起编号（沿用旧模块约定，避免与工作流内 ID 冲突）
LORA_NODE_BASE = 900

# ==================== 分辨率预设 ====================
# zimage 支持任意宽高，但必须为 8 的倍数。
# 命名对齐旧模块的 SIZE_PRESETS 风格：用途_方向
SIZE_PRESETS = {
    # ---- 竖版人像（主流）----
    "portrait":      (1024, 1368),   # 3:4   竖版人像，默认
    "portrait_hd":   (1152, 1536),   # 3:4   高清竖版
    "phone":         (1080, 1920),   # 9:16  手机全屏
    "phone_2k":      (1242, 2688),   # 9:16  手机 2K
    "story":         (1080, 1440),   # 3:4   社媒竖版

    # ---- 方形 ----
    "square":        (1024, 1024),   # 1:1   方图
    "square_hd":     (1368, 1368),   # 1:1   高清方图

    # ---- 横版 ----
    "landscape":     (1368, 768),    # 16:9  横版
    "landscape_hd":  (1920, 1080),   # 16:9  高清横版
    "wide":          (1536, 864),    # 16:9  宽屏

    # ---- 特殊 ----
    "poster":        (768, 1368),    # 9:16  长条海报
    "banner":        (1920, 768),    # 21:9  横幅
}

DEFAULT_SIZE = "portrait"

# ==================== 采样参数 ====================
DEFAULT_STEPS = 8          # turbo 蒸馏模型，8 步足够
DEFAULT_CFG = 1.0          # turbo 必须 cfg=1，否则过曝
DEFAULT_SAMPLER = "res_2s"
DEFAULT_SCHEDULER = "bong_tangent"
DEFAULT_DENOISE = 1.0

# ==================== LoRA 预设 ====================
# 全部 6 个已实测可加载（2026-10-19，工作流 2111844121152663553）
#
# ⚠️ 通用警告（沿用旧模块结论）：
#    任何 LoraLoaderModelOnly 都改 UNET 权重、全局生效，都可能漂移脸型。
#    文生图模式下"脸变了"无法归因，需同 seed 跑「挂 vs 不挂」对照。
#    若脸型必须锁定，唯一可靠手段是图生图锚定（ref_image + 低 denoise）。
KNOWN_LORAS = {
    # ✅ 默认推荐：细节/锐度最高，欧美网红骨相 + 影调偏重
    # ⚠️ 降强度会掉细节：@0.8 → 全局 std 16.93→13.49
    "charm": {
        "file": "zimage-turbo_Cc-风情万种-人像.safetensors",
        "default_strength": 1.0,
        "desc": "风情万种人像，官方权重 0.8-1.2。细节/锐度最高",
    },
    # 去 AI 味 / 无滤镜实拍素人感，推荐步数 10-15
    "nofilter": {
        "file": "无滤真实_V2.safetensors",
        "default_strength": 0.7,
        "recommended_steps": 12,
        "desc": "去 AI 味 / 无滤镜实拍素人感。0.5 原生美颜，0.7-0.9 皮肤纹理真实感强"
                "（建议 10-15 步）",
    },
    # 专治扁平/过曝/塑料感，8 步即可
    "flowdpo": {
        "file": "ZIT-flow-dpo-lora.safetensors",
        "default_strength": 0.8,
        "desc": "Flow-DPO 真实感光影。官方建议 0.6-1.0，>1.5 会过锐过饱和",
    },
    # 全能型模特展示，官方参数强度 0.45 / 步数 10 / cfg 1.1
    "model": {
        "file": "zimage_全能模特.safetensors",
        "default_strength": 0.45,
        "recommended_steps": 10,
        "recommended_cfg": 1.1,
        "desc": "全能型模特展示。官方：强度 0.45 / 步数 10 / cfg 1.1",
    },
    # ⚠️ 会改脸型，仅限不露脸构图
    "peach": {
        "file": "🍑ins-V2-Zimage.safetensors",
        "default_strength": 0.9,
        "desc": "蜜桃臀加强 / 摄影写实。⚠️ 会改脸型，仅限不露脸构图",
    },
    # 二次元动漫风格化
    "zanime": {
        "file": "Z-Anime04.safetensors",
        "default_strength": 1.0,
        "desc": "二次元动漫风格化（Z-Anime04）。建议 0.7-1.0",
    },
}

# 推荐组合（叠加多个 LoRA）
LORA_STACKS = {
    "realistic": ["flowdpo", "nofilter"],       # 真实感光影 + 去 AI 味
    "portrait_hd": ["charm", "flowdpo"],        # 高细节人像 + 光影
    "anime": ["zanime"],                        # 二次元
    "clean": [],                                # 不挂（纯基座）
}


# ==================== 校验 ====================
def _check_size(w, h):
    """zimage 要求宽高为 8 的倍数。"""
    w, h = int(w), int(h)
    if w % 8 or h % 8:
        raise ValueError(f"宽高必须是 8 的倍数，收到 {w}x{h}")
    if w < 256 or h < 256:
        raise ValueError(f"宽高不能小于 256，收到 {w}x{h}")
    if w * h > 4096 * 4096:
        raise ValueError(f"宽高过大（>{4096*4096} 像素），收到 {w}x{h}")
    return w, h


def resolve_size(size):
    """
    解析尺寸参数。支持三种写法：
      - 预设名：'portrait' / 'phone' / 'square' ...
      - 元组：  (1024, 1368)
      - 字符串：'1024x1368' / '1024*1368'
    返回 (w, h)
    """
    if isinstance(size, (tuple, list)):
        return _check_size(*size)
    if isinstance(size, str):
        s = size.strip().lower()
        if s in SIZE_PRESETS:
            return SIZE_PRESETS[s]
        for sep in ("x", "*", ","):
            if sep in s:
                a, b = s.split(sep, 1)
                return _check_size(int(a), int(b))
        raise ValueError(f"无法解析尺寸: {size!r}（可用预设: {', '.join(SIZE_PRESETS)}）")
    raise TypeError(f"size 类型不支持: {type(size)}")


def resolve_lora(spec):
    """
    解析单个 LoRA 规格，返回 (filename, strength) 或 None。
    支持：
      - 预设名：'charm'
      - (名或文件名, 强度)：('charm', 0.9)
      - 文件名直接给：'xxx.safetensors'（强度用 0.9 默认）
    """
    if not spec:
        return None
    if isinstance(spec, (tuple, list)):
        name, strength = spec[0], (spec[1] if len(spec) > 1 else None)
    else:
        name, strength = spec, None

    if name in KNOWN_LORAS:
        info = KNOWN_LORAS[name]
        fn = info["file"]
        st = strength if strength is not None else info["default_strength"]
    else:
        # 当作文件名
        fn = name
        st = strength if strength is not None else 0.9
    return fn, float(st)


def auto_tune(nora_specs):
    """
    根据挂载的 LoRA 自动推断推荐步数 / CFG。
    返回 (steps, cfg, notes)。notes 为调整说明列表。

    规则：
      - 取所有 LoRA 的 recommended_steps 最大值（多重 LoRA 取最保守的值）
      - 同理 recommended_cfg
      - 没有推荐值的 LoRA（charm/flowdpo/peach/zanime）不影响
    """
    steps_list = []
    cfg_list = []
    notes = []
    for spec in nora_specs:
        if not spec:
            continue
        name = spec[0] if isinstance(spec, (list, tuple)) else spec
        info = KNOWN_LORAS.get(name)
        if not info:
            continue
        rs = info.get("recommended_steps")
        if rs:
            steps_list.append(rs)
            notes.append(f"{name} 建议 {rs} 步")
        rc = info.get("recommended_cfg")
        if rc:
            cfg_list.append(rc)
            notes.append(f"{name} 建议 cfg {rc}")
    return (max(steps_list) if steps_list else None,
            max(cfg_list) if cfg_list else None,
            notes)


def _expand_lora(lora):
    """把 lora 参数展开成 spec 列表（不含强度解析）。"""
    if not lora:
        return []
    if isinstance(lora, str) and lora in LORA_STACKS:
        return list(LORA_STACKS[lora])
    if isinstance(lora, (list, tuple)) and not (
            len(lora) == 2 and isinstance(lora[1], (int, float))):
        return list(lora)
    return [lora]


# ==================== 构造 prompt ====================
def build_prompt(
    prompt_text,
    size=DEFAULT_SIZE,
    steps=None,
    cfg=None,
    sampler=DEFAULT_SAMPLER,
    scheduler=DEFAULT_SCHEDULER,
    denoise=DEFAULT_DENOISE,
    seed=None,
    lora=None,
    lora_strength=None,
    negative="",
    auto=True,
    workflow_id=WORKFLOW_ID,
):
    """
    构造完整可提交的 prompt。

    参数:
        prompt_text:  正向提示词
        size:         预设名 / (w,h) / '1024x1368'，见 SIZE_PRESETS
        steps:        采样步数。None 时按 LoRA 自动推荐（auto=True），否则用 DEFAULT_STEPS
        cfg:          CFG。None 时按 LoRA 自动推荐（auto=True），否则用 DEFAULT_CFG
        lora:         单个 LoRA（见 resolve_lora）或列表叠加
                      也可传 LORA_STACKS 的键（如 'realistic'）
        lora_strength: 单 LoRA 时的强度（lora 传列表时忽略）
        negative:     负向提示词，默认空
        auto:         是否根据 LoRA 自动调整步数/CFG（默认 True）
                      显式传 steps/cfg 时以传入值为准
    返回:
        (prompt_dict, meta)  meta 含实际使用的 w/h/seed/lora 等，便于日志
    """
    w, h = resolve_size(size)
    if seed is None:
        seed = random.randint(0, 2**48)

    specs = _expand_lora(lora)

    # ---- 自动调参（仅在未显式指定时生效）----
    auto_notes = []
    if auto:
        rec_steps, rec_cfg, notes = auto_tune(specs)
        auto_notes = notes
        if steps is None and rec_steps:
            steps = rec_steps
        if cfg is None and rec_cfg:
            cfg = rec_cfg
    if steps is None:
        steps = DEFAULT_STEPS
    if cfg is None:
        cfg = DEFAULT_CFG

    p = Z.get_workflow_prompt(workflow_id)

    # ---- 提示词 ----
    p[NODE["pos"]]["inputs"]["text"] = prompt_text or ""
    p[NODE["neg"]]["inputs"]["text"] = negative or ""

    # ---- 尺寸 ----
    p[NODE["empty"]]["inputs"]["width"] = w
    p[NODE["empty"]]["inputs"]["height"] = h
    p[NODE["empty"]]["inputs"]["batch_size"] = 1

    # ---- 采样 ----
    ks = p[NODE["ksampler"]]["inputs"]
    ks["steps"] = int(steps)
    ks["cfg"] = float(cfg)
    ks["sampler_name"] = sampler
    ks["scheduler"] = scheduler
    ks["denoise"] = float(denoise)
    ks["seed"] = int(seed)

    # ---- LoRA 叠加 ----
    # lora 可以是：
    #   None / ''            → 不挂
    #   'charm'              → 单个预设
    #   ('charm', 0.9)       → 单个 + 强度
    #   ['flowdpo','nofilter'] → 叠加（顺序即挂载顺序）
    #   'realistic'          → LORA_STACKS 里的组合
    applied = []
    if specs:
        prev = [NODE["unet"], 0]
        for i, spec in enumerate(specs):
            if not spec:
                continue
            # 单 LoRA 且传了 lora_strength 时覆盖强度
            if len(specs) == 1 and lora_strength is not None:
                r = resolve_lora((spec[0] if isinstance(spec, (list, tuple)) else spec,
                                  lora_strength))
            else:
                r = resolve_lora(spec)
            if not r:
                continue
            fn, st = r
            nid = str(LORA_NODE_BASE + i)
            p[nid] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"lora_name": fn, "strength_model": st, "model": prev},
            }
            prev = [nid, 0]
            applied.append({"node": nid, "file": fn, "strength": st})
        ks["model"] = prev
    else:
        ks["model"] = [NODE["unet"], 0]

    meta = {
        "workflow_id": workflow_id,
        "width": w, "height": h,
        "steps": int(steps), "cfg": float(cfg),
        "sampler": sampler, "scheduler": scheduler, "denoise": float(denoise),
        "seed": int(seed),
        "loras": applied,
        "negative": negative or "",
        "auto_notes": auto_notes,
    }
    return p, meta


# ==================== 便捷运行 ====================
def run(
    prompt_text,
    size=DEFAULT_SIZE,
    steps=None,
    lora=None,
    lora_strength=None,
    cfg=None,
    sampler=DEFAULT_SAMPLER,
    scheduler=DEFAULT_SCHEDULER,
    negative="",
    seed=None,
    auto=True,
    out_dir="workflows/webui_out",
    verbose=True,
):
    """一步到位：出图 → 下载。返回 (本地路径, meta)。"""
    p, meta = build_prompt(
        prompt_text, size=size, steps=steps, cfg=cfg, sampler=sampler,
        scheduler=scheduler, denoise=1.0, seed=seed, lora=lora,
        lora_strength=lora_strength, negative=negative, auto=auto,
    )
    if verbose:
        print(f"[参数] {meta['width']}x{meta['height']} steps={meta['steps']} "
              f"cfg={meta['cfg']} {meta['sampler']}/{meta['scheduler']} seed={meta['seed']}")
        if meta["auto_notes"]:
            print(f"[自动] {'; '.join(meta['auto_notes'])}")
        if meta["loras"]:
            for l in meta["loras"]:
                print(f"[LoRA] {l['file']} @ {l['strength']} (node {l['node']})")
        else:
            print("[LoRA] 无")

    pid = Z.submit_prompt(p)
    if verbose:
        print(f"[提交] prompt_id={pid}")
    entry = Z.poll_history(pid, interval=3, max_wait=600)
    outs = Z.extract_outputs(entry)
    if not outs:
        raise RuntimeError(f"无产物，status={entry.get('status')}")
    path = Z.download_output(outs[0], dest_dir=out_dir)
    meta["prompt_id"] = pid
    meta["local_path"] = path
    if verbose:
        print(f"[完成] {path}")
    return path, meta


def list_sizes():
    """打印所有分辨率预设。"""
    lines = []
    for name, (w, h) in SIZE_PRESETS.items():
        ratio = round(w / h, 3)
        lines.append(f"  {name:14} {w:>5}x{h:<5} 比例 {ratio}")
    return "\n".join(lines)


def list_loras():
    """打印所有 LoRA 预设。"""
    lines = []
    for name, info in KNOWN_LORAS.items():
        lines.append(f"  {name:10} @{info['default_strength']:<4} {info['file']}")
        lines.append(f"             {info['desc']}")
    lines.append("")
    lines.append("  推荐组合:")
    for name, stack in LORA_STACKS.items():
        lines.append(f"    {name:12} = {stack if stack else '（不挂）'}")
    return "\n".join(lines)


if __name__ == "__main__":
    print("=== 分辨率预设 ===")
    print(list_sizes())
    print()
    print("=== LoRA 预设 ===")
    print(list_loras())
