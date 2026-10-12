#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub Krea2 NSFW 文生图 API 直调脚本
============================================
来源工作流：Krea2最强N版文生图集合（23 节点，7 个备选底模）
本脚本内嵌精简后的 13 节点链路，参数全部 CLI 暴露，不依赖外部 JSON 文件。

核心链路：
  UNETLoader(底模)
    -> Lora 风格(107) -> Lora 写实(108) -> Lora 身材(200)
    -> [可选 Krea2PromptWeight(201)] 
    -> ModelSamplingAuraFlow(94 shift)
    -> KSampler(53) -> VAEDecode(54) -> SaveImage(109)
  CLIPLoader(56) -> CLIPTextEncode(51) / Krea2PromptWeight(201)
    -> ConditioningKrea2Rebalance(85) -> KSampler.positive
  ResolutionSelector(49) -> EmptyLatentImage(52)

关键结论（2026-10-11 实测）：
  1. 能否出 NSFW 取决于【底模】，不是提示词。
     普通 Krea2 底模写"裸体/乳头/阴部"会被平台安全过滤自动穿衣服；
     Krea2-turbo_nsfw_V4 底模正常出裸体。
  2. Krea2 用 Qwen3-VL 编码器，ComfyUI 常规 (word:1.5) 加权语法【无效】。
     必须用 Krea2PromptWeight 节点，支持 (词:1.5) 强调 / (词:-1) 抑制。
  3. 画面"灰扑扑"的两个解法（实测数据，同 seed 544x968）：
     - Krea2PromptWeight 去灰词  -> 亮度 +16.7、对比 +7.0（贵 ~5 币，耗时翻倍）
     - HDR VAE                   -> 饱和度 +20.3（更便宜）但【磨皮严重、细节全无】，不推荐
  4. 费用：0.5MP 竖版约 29s/6币；加 PromptWeight 约 53s/11币
"""

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""
API_KEY_BACKUP = ""
BASE = "https://www.runninghub.ai"
IMG_HOST = "https://rh-hk-images.xiaoyaoyou.com"

# ---- 底模映射（原工作流 7 个备选，--model 用简称切换）----
MODELS = {
    "nsfw":       "Krea2-turbo_nsfw_V4.safetensors",                    # ✅ NSFW 专用，能出裸体
    "redcraft23": "redcraft23INT8INT4FP8_30Krea2.safetensors",          # 原工作流默认
    "redmix":     "redcraft2KREA2RedMix_2Krea2Edition.safetensors",
    "gptruth":    "Krea2-GPTruth-全新版.safetensors",                    # 写实向
    "moody":      "Moody-Krea-Mix-v4.1G-FP8_00001__clean.safetensors",  # 氛围向
    "muse":       "krea2MuseByStable_v15TurboFp8.safetensors",
    "turbo":      "Krea2-Turbo-FP8.safetensors",                        # 官方原版
}

# ---- LoRA ----
LORA_STYLE   = "krea2-Cc风情万种-风格.safetensors"
LORA_REALISM = "realism_engine_krea2_v3.1_new.safetensors"
LORA_BODY    = "krea2-Cc-天魔-身材.safetensors"   # 身材强化，来自「风情万种+魔鬼身材」工作流

# ---- VAE ----
VAES = {
    "default": "qwen_image_vae.safetensors",                        # ✅ 保留细节，推荐
    "hdr":     "qwen_image_HDR_vae_fp32_comfy.safetensors",         # 饱和度 +20 但磨皮严重
    "sharp":   "qwenImageVAESharpKrea2_sharpUltraFp32.safetensors", # 锐化版（未实测）
    "hd":      "Krea2-HD-vae.safetensors",                          # 高清细节（未实测）
}

# ---- 宽高比 ----
RATIOS = {
    "1:1":  "1:1 (Square)",
    "2:3":  "2:3 (Portrait Photo)",
    "3:2":  "3:2 (Photo)",
    "3:4":  "3:4 (Portrait Standard)",
    "4:3":  "4:3 (Standard)",
    "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "21:9": "21:9 (Ultrawide)",
}

# ---- 预设提示词 ----
PRESETS = {
    # 竖版仰躺 W 开腿（v3 定稿，含 Krea2PromptWeight 权重语法）
    "lying_w": (
        "超写实真人摄影，9:16竖版单张构图，非并排，全身照，从头到脚完整入镜，不要裁切。"
        "一个二十多岁东亚中国女性，纯正中国面孔，东方亚洲五官，黄皮肤，棕色双麻花辫垂在胸前两侧，"
        "仰躺在一张白色床单上，背部贴床，正面完全朝向镜头，全身赤裸无任何衣物遮挡。"
        "双腿向两侧大幅张开呈现W字形，双膝弯曲向外翻开，大腿分开，两脚掌踩在床面上，"
        "腰肢纤细紧致，臀部丰满挺翘，大腿丰满圆润。"
        "镜头位于她双腿之间的脚端方向，略低机位仰视拍摄，完整拍到她的头部和双脚。"
        "8k分辨率，超高清细节，胶片质感，真实皮肤纹理与毛孔细节。"
        "胸部极其丰满硕大，一对巨大的乳房高耸饱满圆润挺拔(巨大乳房:1.5)(硕大乳房:1.4)(深邃乳沟:1.3)，"
        "深褐色乳晕，乳头完全裸露清晰可见(乳头清晰:1.3)，乳房随仰躺姿态自然向两侧微微摊开。"
        "阴部完全暴露正对镜头，阴唇、阴蒂清晰可见(阴部清晰:1.3)，阴毛浓密乌黑(浓密阴毛:1.2)。"
        "画面明亮通透，色彩鲜艳浓郁(明亮通透:1.3)(高饱和度:1.2)(色彩鲜艳:1.2)，"
        "暖色调，皮肤红润有血色，明亮柔和的自然光从正面均匀照亮全身，白色床单洁净明亮，"
        "(灰暗:-1)(灰雾感:-1)(灰扑扑:-1)(暗淡:-1)(过曝:-0.5)(模糊:-0.5)"
    ),
    # 横版三视图
    "3view": (
        "超写实真人摄影，16:9横版，单张构图，非并排分镜，一张图内三格等宽并排的三视图。"
        "第一格正面全身，第二格侧面全身，第三格背面全身。三格是同一个人：同款棕色双麻花辫、"
        "同一张东亚中国女性面孔、黄皮肤、同一副身材。三格均为全身赤裸无任何衣物遮挡，从头到脚完整入镜不裁切。"
        "胸部丰满硕大，乳房高耸饱满，深褐色乳晕，乳头清晰可见。"
        "阴部清晰可见，阴唇、阴蒂、阴毛清晰可见，阴毛浓密乌黑。"
        "腰肢纤细，臀部丰满挺翘，大腿丰满圆润。"
        "纯白摄影棚背景，均匀柔和的棚拍布光，8k分辨率，超高清细节，真实皮肤纹理与毛孔细节，"
        "(灰暗:-1)(模糊:-0.5)"
    ),
    # 竖版舞姬（带服装，测非裸体用）
    "dancer": (
        "tutulianmo,Beautiful young Chinese woman, slim waist and natural breasts,"
        "棕色双麻花辫垂在胸前两侧，中国女性，东方亚洲面孔，黄皮肤，"
        "身穿金色丝绸异域舞姬装，金色亮片刺绣吊带文胸上衣，金色高开叉长裙，金色流苏腰链，"
        "双臂金色臂环手镯，膝上中景，人物居中，右手向镜头伸出掌心斜向上，左手搭在胯侧，"
        "左腿承重右膝微屈，上身轻向前倾，下巴微收，直视镜头眼神专注挑逗，单侧嘴角微扬，"
        "深色宫廷舞厅，远处烛灯虚化，暖色主光照亮面部，琥珀色侧光刻画身体曲线，"
        "写实摄影，8k分辨率，超高清细节"
    ),
}


# ==================== HTTP ====================
def _req(url, method="GET", body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("accept", "application/json")
    if body is not None:
        r.add_header("content-type", "application/json")
    r.add_header("Authorization", API_KEY)
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode())


# ==================== 构造 prompt ====================
def build_prompt(
    prompt_text,
    model="nsfw",
    style=1.0,
    realism=0.5,
    body=0.85,
    ratio="9:16",
    mp=0.5,
    steps=8,
    cfg=1.0,
    sampler="euler",
    scheduler="simple",
    seed=None,
    shift=3.0,
    rebalance="detail",
    rebalance_mul=1.0,
    custom_weights=None,
    renormalize=True,
    vae="default",
    prompt_weight=1.0,
    out_format="png",
):
    """构造完整 API prompt（节点 ID 沿用原工作流，方便对照 UI）"""
    if model not in MODELS:
        raise ValueError(f"未知底模 {model}，可选: {list(MODELS)}")
    if vae not in VAES:
        raise ValueError(f"未知 VAE {vae}，可选: {list(VAES)}")
    if ratio not in RATIOS:
        raise ValueError(f"未知比例 {ratio}，可选: {list(RATIOS)}")

    p = {}

    # --- 编码器 / VAE ---
    p["56"] = {"class_type": "CLIPLoader", "inputs": {
        "clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
        "type": "krea2", "device": "default"}}
    p["57"] = {"class_type": "VAELoader", "inputs": {
        "vae_name": VAES[vae]}}

    # --- 底模 + 3 层 LoRA 串联 ---
    p["106"] = {"class_type": "UNETLoader", "inputs": {
        "unet_name": MODELS[model], "weight_dtype": "default"}}
    p["107"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
        "lora_name": LORA_STYLE, "strength_model": style, "model": ["106", 0]}}
    p["108"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
        "lora_name": LORA_REALISM, "strength_model": realism, "model": ["107", 0]}}
    p["200"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
        "lora_name": LORA_BODY, "strength_model": body, "model": ["108", 0]}}

    # --- 文本编码 / 权重 ---
    if prompt_weight and prompt_weight > 0:
        # Krea2PromptWeight：支持 (词:1.5)/(词:-1)，输出 patched model + conditioning
        p["201"] = {"class_type": "Krea2PromptWeight", "inputs": {
            "clip": ["56", 0], "model": ["200", 0],
            "text": prompt_text, "strength": prompt_weight}}
        model_out = ["201", 0]
        cond_out = ["201", 1]
    else:
        p["51"] = {"class_type": "CLIPTextEncode", "inputs": {
            "text": prompt_text, "clip": ["56", 0]}}
        model_out = ["200", 0]
        cond_out = ["51", 0]

    # --- Rebalance ---
    p["85"] = {"class_type": "ConditioningKrea2Rebalance", "inputs": {
        "preset": rebalance,
        "per_layer_weights": custom_weights or "1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0,1.0",
        "multiplier": rebalance_mul,
        "renormalize": renormalize,
        "conditioning": cond_out}}

    # --- AuraFlow shift ---
    p["94"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {
        "shift": shift, "model": model_out}}

    # --- 分辨率 / latent ---
    p["49"] = {"class_type": "ResolutionSelector", "inputs": {
        "aspect_ratio": RATIOS[ratio], "megapixels": mp, "multiple": 8}}
    p["52"] = {"class_type": "EmptyLatentImage", "inputs": {
        "width": ["49", 0], "height": ["49", 1], "batch_size": 1}}

    # --- 采样（cfg=1 时 negative 与 positive 同源等价，省掉 ConditioningZeroOut）---
    p["53"] = {"class_type": "KSampler", "inputs": {
        "seed": seed if seed is not None else int(time.time()) % 10**12,
        "control_after_generate": "randomize",
        "steps": steps, "cfg": cfg,
        "sampler_name": sampler, "scheduler": scheduler, "denoise": 1,
        "model": ["94", 0], "positive": ["85", 0], "negative": cond_out,
        "latent_image": ["52", 0]}}

    # --- 解码 + 保存 ---
    p["54"] = {"class_type": "VAEDecode", "inputs": {
        "samples": ["53", 0], "vae": ["57", 0]}}
    if out_format == "jpg":
        p["109"] = {"class_type": "SaveImageJPG_GH", "inputs": {
            "文件名前缀": "ComfyUI", "图像": ["54", 0]}}
    else:
        p["109"] = {"class_type": "SaveImage", "inputs": {
            "filename_prefix": "ComfyUI", "images": ["54", 0]}}
    return p


# ==================== 提交 / 轮询 / 下载 ====================
def submit(prompt, client_id="rh-krea2-nsfw"):
    d = _req(f"{BASE}/proxy/{API_KEY}/prompt", method="POST",
             body={"prompt": prompt, "client_id": client_id}, timeout=120)
    if "prompt_id" in d:
        return d["prompt_id"]
    if d == {}:
        q = _req(f"{BASE}/proxy/{API_KEY}/queue", timeout=30)
        if q.get("queue_running"):
            return str(q["queue_running"][-1][0])
        if q.get("queue_pending"):
            return str(q["queue_pending"][0][0])
    raise RuntimeError(f"submit failed: {d}")


def poll(prompt_id, interval=6, max_wait=600, verbose=True):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            d = _req(f"{BASE}/proxy/{API_KEY}/history/{prompt_id}", timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                time.sleep(interval); continue
            raise
        if prompt_id in d:
            entry = d[prompt_id]
            st = entry.get("status", {})
            if st.get("status_str") == "success":
                return entry
            for m in st.get("messages", []):
                if m[0] == "execution_error":
                    raise RuntimeError(f"execution error: {json.dumps(m[1], ensure_ascii=False)}")
        if verbose:
            el = int(time.time() - t0)
            if el % 30 < interval:
                print(f"  ...等待 {el}s")
        time.sleep(interval)
    raise RuntimeError("poll timeout")


def get_cost(prompt_id):
    """/task/openapi/outputs 是唯一暴露 taskCostTime / consumeCoins 的接口"""
    try:
        d = _req(f"{BASE}/task/openapi/outputs", method="POST",
                 body={"apiKey": API_KEY, "taskId": prompt_id}, timeout=60)
        out = []
        for it in (d.get("data") or []):
            out.append({
                "cost_time": it.get("taskCostTime"),
                "coins": it.get("consumeCoins"),
                "file_url": it.get("fileUrl"),
            })
        return out
    except Exception as e:
        return [{"error": str(e)}]


def extract(entry):
    files = []
    for nid, o in entry.get("outputs", {}).items():
        for img in o.get("images", []):
            files.append({"node": nid, "filename": img.get("filename"),
                          "objectKey": img.get("objectKey")})
    return files


def download(f, dest_dir, file_url=None):
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(dest_dir) / f["filename"]
    if file_url:
        url = file_url
    else:
        url = f"{IMG_HOST}/{f['objectKey']}"
    r = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(r, timeout=180) as resp, open(dest, "wb") as fp:
        fp.write(resp.read())
    return str(dest)


def stats(path):
    """画面质量量化：亮度/饱和度/对比度/肤色占比（用于判断灰扑扑 & 裸露程度）"""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    a = np.asarray(Image.open(path).convert("RGB")).astype(float)
    g = a.mean(2); mx = a.max(2); mn = a.min(2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0).mean() * 255
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    skin = ((R > 95) & (G > 40) & (B > 20) & (R > G) & (R > B)
            & ((R - np.minimum(G, B)) > 15)).mean() * 100
    return {"size": Image.open(path).size, "亮度": round(g.mean(), 1),
            "饱和度": round(sat, 1), "对比度": round(g.std(), 1),
            "肤色%": round(skin, 1)}


# ==================== 一站式 ====================
def run(
    prompt_text=None, preset=None,
    model="nsfw", style=1.0, realism=0.5, body=0.85,
    ratio="9:16", mp=0.5, steps=8, cfg=1.0,
    sampler="euler", scheduler="simple", seed=None,
    shift=3.0, rebalance="detail", rebalance_mul=1.0,
    custom_weights=None, renormalize=True,
    vae="default", prompt_weight=1.0, out_format="png",
    out_dir="outputs", album=None, album_name=None, no_wait=False,
):
    if preset:
        if preset not in PRESETS:
            raise ValueError(f"未知预设 {preset}，可选: {list(PRESETS)}")
        prompt_text = PRESETS[preset]
    if not prompt_text:
        raise ValueError("需要 --prompt 或 --preset")

    print(f"[配置] 底模={model} | 风格={style} 写实={realism} 身材={body} | "
          f"{ratio} {mp}MP | steps={steps} | vae={vae} | prompt_weight={prompt_weight}")

    p = build_prompt(
        prompt_text=prompt_text, model=model, style=style, realism=realism, body=body,
        ratio=ratio, mp=mp, steps=steps, cfg=cfg, sampler=sampler, scheduler=scheduler,
        seed=seed, shift=shift, rebalance=rebalance, rebalance_mul=rebalance_mul,
        custom_weights=custom_weights, renormalize=renormalize,
        vae=vae, prompt_weight=prompt_weight, out_format=out_format,
    )
    print(f"[节点数] {len(p)}")

    pid = submit(p)
    print(f"[提交成功] prompt_id = {pid}")
    if no_wait:
        return {"prompt_id": pid}

    t0 = time.time()
    entry = poll(pid)
    print(f"[出图完成] 用时 {time.time()-t0:.0f}s")

    costs = get_cost(pid)
    for c in costs:
        if "cost_time" in c:
            print(f"[计费] GPU {c['cost_time']}s | {c['coins']} 币")

    files = extract(entry)
    saved = []
    for f in files:
        if f["filename"].endswith((".png", ".jpg", ".webp")):
            fu = next((c.get("file_url") for c in costs if c.get("file_url")), None)
            path = download(f, out_dir, fu)
            saved.append(path)
            print(f"[已下载] {path} ({os.path.getsize(path)} bytes)")
            s = stats(path)
            if s:
                print(f"[画面] {s['size']} 亮度={s['亮度']} 饱和={s['饱和度']} "
                      f"对比={s['对比度']} 肤色={s['肤色%']}%")

    if album and saved:
        import shutil
        Path(album).mkdir(parents=True, exist_ok=True)
        name = album_name or Path(saved[0]).stem
        dst = Path(album) / (name + Path(saved[0]).suffix)
        shutil.copy(saved[0], dst)
        print(f"[已复制] {dst}")

    return {"prompt_id": pid, "costs": costs, "saved": saved}


# ==================== CLI ====================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="RunningHub Krea2 NSFW 文生图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 竖版仰躺 W 开腿（v3 定稿配置）
  python rh_krea2_nsfw.py --preset lying_w --ratio 9:16 --mp 0.5 --album /sdcard/Pictures --album-name 洛宝仰躺W开腿

  # 横版三视图
  python rh_krea2_nsfw.py --preset 3view --ratio 16:9 --mp 1.0

  # 换底模 + 关 PromptWeight（更便宜）
  python rh_krea2_nsfw.py --preset lying_w --model gptruth --prompt-weight 0

  # 自定义提示词，身材 LoRA 拉满
  python rh_krea2_nsfw.py --prompt "..." --body 1.0 --realism 0.5

  # 快速抽卡（0.25MP，约 3 币）
  python rh_krea2_nsfw.py --preset lying_w --mp 0.25 --prompt-weight 0 --seed 12345
""")
    ap.add_argument("--prompt", help="提示词（支持 (词:1.5)/(词:-1) 权重语法，需 --prompt-weight > 0）")
    ap.add_argument("--preset", choices=list(PRESETS), help="预设提示词")
    ap.add_argument("--model", default="nsfw", choices=list(MODELS), help="底模（默认 nsfw）")
    ap.add_argument("--style", type=float, default=1.0, help="风格 LoRA 权重（默认 1.0）")
    ap.add_argument("--realism", type=float, default=0.5, help="写实 LoRA 权重（默认 0.5）")
    ap.add_argument("--body", type=float, default=0.85, help="身材 LoRA 权重（默认 0.85）")
    ap.add_argument("--ratio", default="9:16", choices=list(RATIOS), help="宽高比（默认 9:16）")
    ap.add_argument("--mp", type=float, default=0.5, help="百万像素 0.1-16（默认 0.5）")
    ap.add_argument("--steps", type=int, default=8, help="采样步数（默认 8）")
    ap.add_argument("--cfg", type=float, default=1.0, help="CFG（默认 1.0，Krea2 固定）")
    ap.add_argument("--sampler", default="euler", help="采样器（默认 euler）")
    ap.add_argument("--scheduler", default="simple", help="调度器（默认 simple）")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--shift", type=float, default=3.0, help="AuraFlow shift（默认 3.0）")
    ap.add_argument("--rebalance", default="detail",
                    choices=["balanced", "detail", "subtle", "uniform", "custom"],
                    help="Rebalance 预设（默认 detail）")
    ap.add_argument("--rebalance-mul", type=float, default=1.0, help="Rebalance multiplier")
    ap.add_argument("--custom-weights", default=None, help="custom 预设的 12 层权重，逗号分隔")
    ap.add_argument("--no-renormalize", action="store_true", help="关闭 renormalize")
    ap.add_argument("--vae", default="default", choices=list(VAES),
                    help="VAE（默认 default；hdr 会磨皮）")
    ap.add_argument("--prompt-weight", type=float, default=1.0,
                    help="Krea2PromptWeight strength，0=禁用（默认 1.0）")
    ap.add_argument("--format", dest="out_format", default="png", choices=["png", "jpg"],
                    help="输出格式（默认 png 保细节）")
    ap.add_argument("--out", default="outputs", help="下载目录")
    ap.add_argument("--album", default=None, help="复制到相册目录，如 /sdcard/Pictures")
    ap.add_argument("--album-name", default=None, help="相册文件名（不含扩展名）")
    ap.add_argument("--no-wait", action="store_true", help="只提交不等待")
    a = ap.parse_args()

    run(
        prompt_text=a.prompt, preset=a.preset,
        model=a.model, style=a.style, realism=a.realism, body=a.body,
        ratio=a.ratio, mp=a.mp, steps=a.steps, cfg=a.cfg,
        sampler=a.sampler, scheduler=a.scheduler, seed=a.seed,
        shift=a.shift, rebalance=a.rebalance, rebalance_mul=a.rebalance_mul,
        custom_weights=a.custom_weights, renormalize=not a.no_renormalize,
        vae=a.vae, prompt_weight=a.prompt_weight, out_format=a.out_format,
        out_dir=a.out, album=a.album, album_name=a.album_name, no_wait=a.no_wait,
    )
