#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub Zimage 图生图/文生图 API 直调脚本
================================================
核心思路：走 Native ComfyUI 代理端点 `/proxy/<api-key>`，
绕过 openapi getJsonApiFormat 的序列化过滤（LoadImage/VAEEncode 保存时被丢弃）。

适用工作流：2108658505526910977（Zimage_Turbo尺寸控制器版）
核心节点：
  - 1  UNETLoader        z_image_turbo_bf16.safetensors
  - 14 LoraLoaderModelOnly  瑶光 LoRA (0.3)
  - 20 LoraLoaderModelOnly  如梦似幻 LoRA (0.25)
  - 22 CLIPLoader        qwen_3_4b.safetensors (lumina2)
  - 5  CLIPTextEncode    提示词
  - 6  ConditioningZeroOut  negative
  - 3  VAELoader
  - 4  KSampler          steps 默认 8
  - 7  EmptyLatentImage  文生图 latent
  - 16/17 Int            尺寸控制器（宽度/高度）
  - 8  VAEDecode
  - 10 SaveImage
图生图模式（手动构造，绕过保存过滤）：
  - 24 LoadImage   参考图输入（/proxy/upload/image 上传后文件名）
  - 26 ImageScale  参考图归一到 width×height（2026-10-11 新增，防输出尺寸飘）
  - 25 VAEEncode   pixels ← ImageScale.IMAGE, vae ← VAE(3)
  - KSampler(4).latent_image ← VAEEncode(25).LATENT
  - denoise 语义（2026-10-11 实测）：
      0.5-0.6 → 锁姿势锁构图，提示词几乎不生效
      0.65-0.75 → 换人物/画风的最佳区间（2D→真人用 0.7）
      0.85+   → 提示词才真正改写场景/服装/姿势（但脸锚定会松）

主力配方：2D/3D 图 → 真人 + 换洛宝（详见 skill D.10）
  --ref-image <对齐尺寸的参考图> --steps 12 --denoise 0.7 --lora none
  实测 4 例全成功（二次元/3D/真人参考图均可）

已验证（2026-10-10）：
  - 文生图（无水印 /proxy 通道）✅
  - 图生图换头（保留服装/姿势，denoise 0.7/0.55）✅

ControlNet 骨架控制（skill D.11）：⚠️ 已搁置
  - ZImageFunControlnet 链路可跑通，但骨架源质量决定一切
  - 二次元图 OpenPose 检测不准（全黑或几何错位），仅写实照片可靠
  - DWPoseDetectorNode 是付费节点，会拖挂整条工作流，别用

已验证（2026-10-10）：
  - 上传参考图 → input 文件名 ✅
"""

import json
import time
import uuid
import urllib.request
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""   # 优先 key
BASE = "https://www.runninghub.ai"
PROXY = f"{BASE}/proxy/{API_KEY}"

WORKFLOW_ID = "2108658505526910977"

# 节点 ID 映射（来自 getJsonApiFormat + 图生图手动注入）
NODE = {
    "ksampler": "4",        # KSampler
    "clip_text": "5",       # CLIPTextEncode (positive)
    "empty_latent": "7",    # EmptyLatentImage (文生图 latent)
    "vae": "3",             # VAELoader
    "width": "16",          # Int 宽度控制器
    "height": "17",         # Int 高度控制器
    "load_image": "24",     # LoadImage（图生图注入）
    "vae_encode": "25",     # VAEEncode（图生图注入）
    "image_scale": "26",    # ImageScale（图生图尺寸归一，2026-10-11 新增）
}

# ==================== LoRA 注入（2026-10-11 新增）====================
# Zimage 工作流（2108658505526910977）本身【没有 LoRA 节点】，
# KSampler(4).model 直连 UNETLoader(1)，所以这里是纯新增挂载，不存在替换问题。
#
# 挂载链路：UNETLoader(1) -> 新 LoRA(901) -> KSampler(4).model
#
# 与 MiniMaxH3（见 rh_h3_lora.py）不同：那边有 turbo 8step LoRA 必须保留在前面，
# 这边没有 turbo，直接挂在 UNET 后即可。
ZIMAGE_LORA_NODE = "901"

KNOWN_LORAS = {
    # ⚠️ 实测（2026-10-11）：peach 会覆盖人物面部特征，且不是强度问题。
    #    - 文生图 @0.9 背面构图 → 脸不可见，效果 OK
    #    - 图生图 @0.7 denoise=0.55（正面参考图锚定）→ 面部仍被改成 LoRA 的脸型 ❌
    #   根因：LoraLoaderModelOnly 改的是 UNET 权重，全局生效；
    #         conditioning 的 area/mask 限定对权重级 LoRA 无效，无法只作用于身体。
    #   结论：正面露脸场景不要挂它。要臀型改用 Krea2（rh_krea2_nsfw.py 身材 LoRA）
    #        或纯提示词描述（蜜桃形轮廓 / 臀峰高耸 / 腰臀比）。
    "peach": {
        "file": "🍑ins-V2-Zimage.safetensors",
        "default_strength": 0.9,
        "desc": "蜜桃臀加强 / 摄影写实话风加强（🍑V2，基础模型 Z-image-turbo）"
               "⚠️ 会改脸型，仅限不露脸构图使用",
    },
    # ---- 以下为 2026-10-11 搜集的候选，仅登记未实测 ----
    # 共用同一挂载链路（UNETLoader(1) -> 901 -> KSampler(4)）
    #
    # ⚠️ 通用警告：任何 LoraLoaderModelOnly 都改 UNET 权重、全局生效，
    #    都可能漂移脸型。文生图模式下"脸变了"无法归因——需要在同 seed 下
    #    跑「挂 vs 不挂」对照才能判定是 LoRA 还是文生图固有漂移。
    #    若脸型必须锁定，唯一可靠手段是图生图锚定（ref_image + 低 denoise），
    #    但注意：蜜桃臀 LoRA 在 denoise=0.55 下仍覆盖了脸 → 锚定不是万能的。
    "nofilter": {   # Zimage Turbo_无滤真实_V2
        "file": "无滤真实_V2.safetensors",
        "default_strength": 0.7,
        "desc": "去 AI 味 / 无滤镜实拍素人感。作者：0.5 偏原生美颜，0.6 平衡，"
                "0.7-0.9 皮肤纹理真实感强+女性胸围增大。步数 10-15。"
                "⚠️ 2026-10-11 文生图 @0.7 出图后用户反馈脸型变化，"
                "但未做同 seed 对照，无法区分是 LoRA 还是文生图固有漂移",
    },
    "flowdpo": {    # 真实感光影 z-image-turbo-flow-dpo
        "file": "ZIT-flow-dpo-lora.safetensors",
        "default_strength": 0.8,
        "desc": "Flow-DPO 真实感光影。专治扁平/过曝/塑料感，8 步即可。"
                "官方建议 0.6-1.0，>1.5 会过锐过饱和。"
                "⚠️ 非 img2img 修复器，图生图模式效果存疑",
    },
    "model": {      # zimage_turbo_全能模特
        "file": "zimage_全能模特.safetensors",
        "default_strength": 0.45,
        "desc": "全能型模特展示。官方参数：强度 0.45 / 步数 10 / cfg 1.1",
    },
    # ✅ CLI 默认 LoRA（2026-10-11 起）
    #    用户实测三版后确认：charm / nofilter / 原版【都能】出细节，
    #    charm 不是唯一（此为 2026-10-11 修正，此前结论有误）。
    #    选它的理由是用户偏好其风格，不是因为它"唯一能出细节"。
    #    特征：欧美网红骨相 + 影调偏重（亮度112/饱和35/对比59），
    #          细节/锐度最高（全局 std 16.93，三版最高）。
    #    ⚠️ 降强度会掉细节：@0.8 → 私处区 std 18.18→7.62、全局 16.93→13.49。
    #    ⚠️ 提示词对抗无效：权重级 LoRA 不受文字约束（同 peach 改脸机理）。
    #    影调可用本地 PIL 后期修；骨相改不了。
    "charm": {      # zimage-turbo_Cc-风情万种-人像
        "file": "zimage-turbo_Cc-风情万种-人像.safetensors",
        "default_strength": 1.0,
        "desc": "风情万种人像，官方权重 0.8-1.2。CLI 默认（用户偏好）。"
                "细节/锐度三版最高；⚠️ 欧美网红骨相 + 重影调，降强度会掉细节",
    },
}


def apply_lora(p, lora=None, strength=None, verbose=True):
    """
    在 UNETLoader 之后串入一个 LoRA（Zimage 专用）。

    lora:     预设名（如 peach）或直接文件名；None = 不挂
    strength: 强度；None = 用预设默认
    """
    if not lora:
        return p, False

    name, default_strength = KNOWN_LORAS.get(lora, {}).get("file", lora), \
                             KNOWN_LORAS.get(lora, {}).get("default_strength", 0.9)
    st = strength if strength is not None else default_strength

    p[ZIMAGE_LORA_NODE] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "lora_name": name,
            "strength_model": st,
            "model": ["1", 0],      # UNETLoader
        },
    }
    p[NODE["ksampler"]]["inputs"]["model"] = [ZIMAGE_LORA_NODE, 0]

    if verbose:
        print(f"[LoRA] {name} @ {st} → [1] UNET → [{ZIMAGE_LORA_NODE}] → [4] KSampler")
    return p, True

# ==================== 基础请求 ====================
def _request(url, method="GET", body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    if body is not None:
        req.add_header("content-type", "application/json")
    req.add_header("Authorization", API_KEY)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read().decode())


def _upload_multipart(url, file_path, field_name, content_type):
    """通用 multipart 上传（ComfyUI /upload/image 风格）"""
    data = Path(file_path).read_bytes()
    boundary = uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="{field_name}"; filename="{Path(file_path).name}"\r\n'.encode()
    body += f"Content-Type: {content_type}\r\n\r\n".encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("accept", "application/json")
    req.add_header("content-type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", API_KEY)
    resp = urllib.request.urlopen(req, timeout=60)
    return json.loads(resp.read().decode())


# ==================== 1. 上传素材 ====================
def upload_image(file_path: str) -> str:
    """上传参考图，返回 ComfyUI input 文件名（如 493f488e...png）"""
    ct = "image/png" if str(file_path).lower().endswith(".png") else "image/jpeg"
    d = _upload_multipart(f"{PROXY}/upload/image", file_path, "image", ct)
    return d["name"]


# ==================== 2. 拉取并构造 prompt ====================
def get_workflow_prompt(workflow_id: str = WORKFLOW_ID) -> dict:
    """拉取工作流 API JSON，返回 prompt dict"""
    d = _request(
        f"{BASE}/api/openapi/getJsonApiFormat",
        method="POST",
        body={"apiKey": API_KEY, "workflowId": workflow_id},
        timeout=30,
    )
    if d.get("code") != 0:
        raise RuntimeError(f"getJsonApiFormat failed: {d}")
    return json.loads(d["data"]["prompt"])


def build_prompt(
    prompt_text: str = None,
    ref_image: str = None,          # 参考图 input 文件名（图生图模式）
    width: int = 1920,
    height: int = 1080,
    steps: int = 8,
    denoise: float = None,          # 图生图 denoise（默认 0.7；None 则文生图用 1.0）
    seed: int = None,
    workflow_id: str = WORKFLOW_ID,
    lora: str = None,            # LoRA 预设名（如 peach）或文件名；None = 不挂
    lora_strength: float = None, # 强度；None = 用预设默认（peach 为 0.9）
 ) -> dict:
    """
    构造完整可提交的 prompt

    参数:
        prompt_text: 提示词（None 则用工作流现有值）
        ref_image:   参考图文件名（传入则注入 LoadImage+VAEEncode 图生图链路）
        width/height: 画布尺寸（Int 16/17 尺寸控制器）
        steps:        采样步数（默认 8）
        denoise:      图生图重绘强度（默认 0.7）
        seed:         随机种子（None 保持工作流现有值）
    """
    p = get_workflow_prompt(workflow_id)

    # 尺寸
    p[NODE["width"]]["inputs"]["value"] = width
    p[NODE["height"]]["inputs"]["value"] = height

    # 步数
    p[NODE["ksampler"]]["inputs"]["steps"] = steps

    # 种子
    if seed is not None:
        p[NODE["ksampler"]]["inputs"]["seed"] = seed

    # 提示词
    if prompt_text is not None:
        p[NODE["clip_text"]]["inputs"]["text"] = prompt_text

    if ref_image:
        # ---- 图生图模式：LoadImage -> ImageScale(尺寸归一) -> VAEEncode ----
        # ImageScale 是 2026-10-11 新增：参考图尺寸与画布不一致时先归一到 width×height，
        # 否则 VAEEncode 出来的 latent 尺寸和 Int(16/17) 控制器不一致，输出尺寸会飘。
        p[NODE["load_image"]] = {
            "class_type": "LoadImage",
            "inputs": {"image": ref_image},
        }
        p[NODE["image_scale"]] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": [NODE["load_image"], 0],
                "upscale_method": "lanczos",
                "width": width,
                "height": height,
                "crop": "disabled",
            },
        }
        p[NODE["vae_encode"]] = {
            "class_type": "VAEEncode",
            "inputs": {
                "pixels": [NODE["image_scale"], 0],
                "vae": [NODE["vae"], 0],
            },
        }
        p[NODE["ksampler"]]["inputs"]["latent_image"] = [NODE["vae_encode"], 0]
        # denoise 语义（2026-10-11 实测）：
        #   0.5-0.6 → 锁姿势锁构图，只换细节/质感（参考图主导）
        #   0.7-0.8 → 平衡，服装/场景开始跟着提示词走
        #   0.85+   → 构图/场景/姿势被提示词改写，仅保留部分人物特征
        p[NODE["ksampler"]]["inputs"]["denoise"] = denoise if denoise is not None else 0.7
    else:
        # ---- 文生图模式：保持 EmptyLatentImage ----
        p[NODE["ksampler"]]["inputs"]["latent_image"] = [NODE["empty_latent"], 0]
        p[NODE["ksampler"]]["inputs"]["denoise"] = 1.0

    # ---- LoRA（纯新增，挂在 UNETLoader 之后）----
    if lora:
        apply_lora(p, lora=lora, strength=lora_strength)

    return p


# ==================== 3. 提交任务 ====================
def submit_prompt(prompt: dict, client_id: str = "rh-zimage-api") -> str:
    """
    提交到 Native ComfyUI /prompt，返回 prompt_id

    注意：/prompt 返回体偶尔是空 {}（任务已入队但不回显 prompt_id），
    此时从 /queue 拉取最新的 running/pending 任务 id 兜底。
    """
    d = _request(f"{PROXY}/prompt", method="POST", body={"prompt": prompt, "client_id": client_id}, timeout=120)
    if "prompt_id" in d:
        return d["prompt_id"]

    # 兜底：从 queue 拿最新任务 id
    if d == {}:
        q = _request(f"{PROXY}/queue", timeout=30)
        running = q.get("queue_running", [])
        pending = q.get("queue_pending", [])
        if running:
            return str(running[-1][0])
        if pending:
            return str(pending[0][0])
    raise RuntimeError(f"submit failed: {d}")


# ==================== 4. 轮询结果 ====================
class PollTimeout(Exception):
    """轮询超时（排队/生成超过 max_wait），任务可能仍远端正跑，不能判死。"""


def poll_history(prompt_id: str, interval: int = 8, max_wait: int = 1800) -> dict:
    """轮询 /history/<prompt_id> 直到 success/fail，返回完整 history entry"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            d = _request(f"{PROXY}/history/{prompt_id}", timeout=30)
            if prompt_id in d:
                entry = d[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "success":
                    return entry
                for m in status.get("messages", []):
                    if m[0] == "execution_error":
                        raise RuntimeError(f"execution error: {json.dumps(m[1], ensure_ascii=False)}")
            time.sleep(interval)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                time.sleep(interval)
                continue
            raise
    raise PollTimeout(f"poll timeout after {max_wait}s: {prompt_id}")


# ==================== 5. 提取输出与下载 ====================
def extract_outputs(entry: dict) -> list:
    """从 history entry 提取输出文件 objectKey 列表"""
    files = []
    for nid, out in entry.get("outputs", {}).items():
        for img in out.get("images", []):
            files.append({
                "node": nid,
                "filename": img.get("filename"),
                "objectKey": img.get("objectKey"),
                "type": img.get("type"),
            })
    return files


def download_output(output: dict, dest_dir: str = ".") -> str:
    """下载输出文件到本地"""
    object_key = output["objectKey"]
    url = f"https://rh-hk-images.xiaoyaoyou.com/{object_key}"
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(dest_dir) / output["filename"]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())
    return str(dest)


# ==================== 一站式入口 ====================
def run(
    prompt_text: str = None,
    ref_image_path: str = None,     # 本地参考图路径（图生图，自动上传）
    width: int = 1920,
    height: int = 1080,
    steps: int = 8,
    denoise: float = None,
    seed: int = None,
    download_dir: str = "outputs",
    workflow_id: str = WORKFLOW_ID,
    lora: str = None,
    lora_strength: float = None,
    on_submit: callable = None,     # 提交成功回调 on_submit(prompt_id)，用于立即持久化 pid
 ):
    """
    一键执行：上传参考图（可选）→ 构造 prompt → 提交 → 轮询 → 下载

    示例:
        # 文生图（默认洛宝锚点）
        run(prompt_text="洛宝穿睡衣坐在窗边", width=1080, height=1920)

        # 图生图换头（保留参考图服装/姿势，人物转洛宝）
        run(ref_image_path="/path/ref.jpg", prompt_text="穿同款衣服的洛宝")

        # 锁姿势（低 denoise）
        run(ref_image_path="/path/ref.jpg", denoise=0.55)
    """
    # 1. 上传参考图
    ref_image = None
    if ref_image_path:
        ref_image = upload_image(ref_image_path)
        print(f"[上传参考图] {ref_image}")

    # 2. 构造 prompt
    prompt = build_prompt(
        prompt_text=prompt_text,
        ref_image=ref_image,
        width=width,
        height=height,
        steps=steps,
        denoise=denoise,
        seed=seed,
        workflow_id=workflow_id,
        lora=lora,
        lora_strength=lora_strength,
    )

    mode = "图生图" if ref_image else "文生图"
    print(f"[模式] {mode} | {width}x{height} | steps={steps} | denoise={prompt[NODE['ksampler']]['inputs'].get('denoise')}")

    # 3. 提交
    prompt_id = submit_prompt(prompt)
    print(f"[提交成功] prompt_id = {prompt_id}")
    if on_submit:
        on_submit(prompt_id)

    # 4. 轮询
    entry = poll_history(prompt_id)
    print("[执行完成] status = success")

    # 5. 提取 + 下载
    outputs = extract_outputs(entry)
    print(f"[输出文件] {len(outputs)} 个")
    saved = []
    for out in outputs:
        if out["filename"].endswith((".png", ".jpg", ".webp")):
            path = download_output(out, download_dir)
            saved.append(path)
            print(f"  已下载: {path}")
    return {"prompt_id": prompt_id, "outputs": outputs, "saved": saved}


# ==================== CLI ====================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="RunningHub Zimage 图生图/文生图 API 直调")
    ap.add_argument("--prompt", type=str, default=None, help="提示词（默认洛宝锚点+附加描述）")
    ap.add_argument("--ref-image", type=str, default=None, help="本地参考图路径（图生图模式）")
    ap.add_argument("--width", type=int, default=1920, help="画布宽度")
    ap.add_argument("--height", type=int, default=1080, help="画布高度")
    ap.add_argument("--steps", type=int, default=8, help="采样步数（默认 8）")
    ap.add_argument("--denoise", type=float, default=None, help="图生图重绘强度（默认 0.7）")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--out", type=str, default="outputs", help="下载目录")
    # LoRA（纯新增，挂在 UNETLoader 之后）
    # 默认挂 charm（风情万种）：2026-10-11 实测它是唯一能刻画出身体/私处细节的
    # Zimage LoRA，代价是画风偏欧美网红 + 影调偏重。要换风格显式传 --lora。
    ap.add_argument("--lora", type=str, default="charm",
                    help="LoRA 预设名或文件名；默认 charm（风情万种）。"
                         "可选：peach 蜜桃臀 / nofilter 无滤真实 / flowdpo 光影 / "
                         "model 全能模特 / none 不挂")
    ap.add_argument("--lora-strength", type=float, default=None,
                    help="LoRA 强度（默认用预设值，charm 为 1.0）")
    args = ap.parse_args()

    # --lora none / off / 0 → 不挂（原版）
    _lora = args.lora
    if _lora and _lora.lower() in ("none", "off", "0", "false"):
        _lora = None

    run(
        prompt_text=args.prompt,
        ref_image_path=args.ref_image,
        width=args.width,
        height=args.height,
        steps=args.steps,
        denoise=args.denoise,
        seed=args.seed,
        download_dir=args.out,
        lora=_lora,
        lora_strength=args.lora_strength,
    )