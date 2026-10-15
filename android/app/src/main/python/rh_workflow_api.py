#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub 工作流 API 直调脚本（手机端副本）
================================
核心思路：走 Native ComfyUI 代理端点 `/proxy/<api-key>`，
绕过 openapi getJsonApiFormat 的序列化过滤（动态孔 mode.ref_images.* 被丢弃的问题）。

适用工作流：2108502955011149825（minimaxH3超级加速全能工作流）
核心节点：MiniMaxH3Unified (160)，omni_reference 模式

已验证（2026-10-10）：
- /proxy/<key>/upload/image        上传图片 → 返回文件名 ✅
- /proxy/<key>/prompt              提交含动态孔的完整 prompt → prompt_id ✅
- /proxy/<key>/history/<prompt_id> 轮询状态 → success ✅
- 视频输出可下载（COS 直链 200）✅
"""

import json
import time
import uuid
import urllib.request
from pathlib import Path

try:
    from rh_h3_lora import resolve_lora, apply_lora
except ImportError:      # 单文件拷贝时不至于整个挂掉
    resolve_lora = apply_lora = None

# ==================== 配置 ====================
API_KEY = ""   # 优先 key
BASE = "https://www.runninghub.ai"
PROXY = f"{BASE}/proxy/{API_KEY}"

WORKFLOW_ID = "2108502955011149825"

# 节点 ID 映射（来自 getJsonApiFormat）
NODE = {
    "main": "160",          # MiniMaxH3Unified
    "ref_img_0": "251",     # OAILoadImage ×5
    "ref_img_1": "252",
    "ref_img_2": "253",
    "ref_img_3": "254",
    "ref_img_4": "255",
    "ref_video": "256",     # OAILoadVideo
    "ref_audio": "257",     # OAILoadAudio
    "prompt": "144",        # CR Text（提示词）
    "resolution": "115",    # ResolutionSelector
    "noise": "129",         # RandomNoise
    "steps": "204",         # BasicScheduler（8 步）
}

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
    """上传图片，返回 ComfyUI 文件名（如 493f488e...png）"""
    ct = "image/png" if str(file_path).lower().endswith(".png") else "image/jpeg"
    d = _upload_multipart(f"{PROXY}/upload/image", file_path, "image", ct)
    return d["name"]


def upload_video(file_path: str) -> str:
    """上传视频，返回文件名"""
    d = _upload_multipart(f"{PROXY}/upload/video", file_path, "video", "video/mp4")
    return d["name"]


def upload_audio(file_path: str) -> str:
    """上传音频，返回文件名"""
    d = _upload_multipart(f"{PROXY}/upload/audio", file_path, "audio", "audio/mpeg")
    return d["name"]


# ==================== 2. 拉取并构造 prompt ====================
def get_workflow_prompt(workflow_id: str = WORKFLOW_ID) -> dict:
    """拉取工作流 API JSON（不含动态孔），返回 prompt dict"""
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
    ref_images: list = None,      # 图片文件名列表（最多 5 个）
    ref_video: str = None,        # 视频文件名（可选）
    ref_audio: str = None,        # 音频文件名（可选）
    aspect_ratio: str = "9:16 (Portrait Widescreen)",
    megapixels: float = 0.4,
    duration: float = 5,
    steps: int = 8,
    noise_seed: int = None,
    workflow_id: str = WORKFLOW_ID,
    mode: str = None,            # None = 自动（有参考媒体 → omni_reference，无 → text_to_video）
    lora: str = None,            # LoRA 预设名或文件名，None = 不挂
    lora_strength: float = None, # 强度；None = 用预设默认（synthpussy 为 0.9）
) -> dict:
    """
    构造完整可提交的 prompt（补全动态孔连线）

    参数:
        prompt_text: 提示词（None 则用工作流里现有 CR Text）
        ref_images:  最多 5 个图片文件名（OAILoadImage 的 image 值）
        ref_video:   视频文件名（OAILoadVideo 的 video 值）
        ref_audio:   音频文件名（OAILoadAudio 的 audio 值）
        aspect_ratio: 分辨率比例
        megapixels:   分辨率档位
        duration:     视频时长（秒）
        steps:        采样步数
        noise_seed:   随机种子（None 则用工作流现有值）
        mode:         text_to_video / omni_reference / first_last_frame；
                      None = 自动（传了任一参考媒体 → omni_reference，否则 text_to_video）
        lora:         LoRA 预设名或文件名，串在 turbo 之后（新增，不替换）
        lora_strength: LoRA 强度

    注意：不传任何参考媒体 = 文生视频（T2V）。T2V 比同分辨率 I2V 便宜约 4 成
          （实测 10s/608x1056：T2V 303s/61币 vs I2V 528s/106币）。
    """
    p = get_workflow_prompt(workflow_id)

    # ---- 模式：不传参考媒体 → T2V ----
    has_ref = bool(ref_images or ref_video or ref_audio)
    resolved_mode = mode or ("omni_reference" if has_ref else "text_to_video")
    p[NODE["main"]]["inputs"]["mode"] = resolved_mode

    # 提示词
    if prompt_text is not None:
        p[NODE["prompt"]]["inputs"]["text"] = prompt_text

    # 分辨率
    p[NODE["resolution"]]["inputs"]["aspect_ratio"] = aspect_ratio
    p[NODE["resolution"]]["inputs"]["megapixels"] = megapixels

    # 主节点时长
    p[NODE["main"]]["inputs"]["duration"] = duration

    # 种子
    if noise_seed is not None:
        p[NODE["noise"]]["inputs"]["noise_seed"] = noise_seed

    # 步数
    p[NODE["steps"]]["inputs"]["steps"] = steps

    # ---- 参考图（动态孔连线）----
    # 先清掉旧的动态孔键
    for k in list(p[NODE["main"]]["inputs"].keys()):
        if k.startswith("mode.ref_images.") or k.startswith("mode.ref_videos.") \
           or k.startswith("mode.ref_video_audios.") or k.startswith("mode.ref_audios."):
            del p[NODE["main"]]["inputs"][k]

    if ref_images:
        for i, img in enumerate(ref_images[:5]):
            node_id = str(251 + i)   # 251~255
            p[node_id]["inputs"]["image"] = img
            p[NODE["main"]]["inputs"][f"mode.ref_images.ref_image_{i}"] = [node_id, 0]

    # ---- 参考视频 ----
    if ref_video:
        p[NODE["ref_video"]]["inputs"]["video"] = ref_video
        p[NODE["main"]]["inputs"]["mode.ref_videos.ref_video_0"] = [NODE["ref_video"], 0]
        p[NODE["main"]]["inputs"]["mode.ref_video_audios.ref_video_audio_0"] = [NODE["ref_video"], 1]

    # ---- 参考音频 ----
    if ref_audio:
        p[NODE["ref_audio"]]["inputs"]["audio"] = ref_audio
        p[NODE["main"]]["inputs"]["mode.ref_audios.ref_audio_0"] = [NODE["ref_audio"], 0]

    # ---- LoRA（串在 turbo 之后，新增不替换）----
    if lora:
        if apply_lora is None:
            raise RuntimeError("rh_h3_lora.py 不可用（需与本脚本同目录）")
        name, default_strength = resolve_lora(lora)
        strength = lora_strength if lora_strength is not None else default_strength
        p, _ = apply_lora(p, name, strength, workflow_id)

    return p


# ==================== 3. 提交任务 ====================
def submit_prompt(prompt: dict, client_id: str = "rh-api-script") -> str:
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


def poll_history(prompt_id: str, interval: int = 15, max_wait: int = 1800) -> dict:
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
                # 检查执行错误
                for m in status.get("messages", []):
                    if m[0] == "execution_error":
                        raise RuntimeError(f"execution error: {json.dumps(m[1], ensure_ascii=False)}")
            # 还在跑/排队
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
    dest = Path(dest_dir) / output["filename"]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())
    return str(dest)


# ==================== 一站式入口 ====================
def run(
    prompt_text: str = None,
    ref_images: list = None,
    ref_video: str = None,
    ref_audio: str = None,
    ref_image_paths: list = None,   # 本地图片路径（自动上传）
    ref_video_path: str = None,     # 本地视频路径（自动上传）
    ref_audio_path: str = None,     # 本地音频路径（自动上传）
    aspect_ratio: str = "9:16 (Portrait Widescreen)",
    megapixels: float = 0.4,
    duration: float = 5,
    steps: int = 8,
    noise_seed: int = None,
    download_dir: str = "outputs",
    workflow_id: str = WORKFLOW_ID,
    mode: str = None,
    lora: str = None,
    lora_strength: float = None,
    on_submit: callable = None,     # 提交成功回调 on_submit(prompt_id)，用于立即持久化 pid
):
    """
    一键执行：上传素材 → 构造 prompt → 提交 → 轮询 → 下载

    示例:
        run(
            prompt_text="一只猫在窗台上打哈欠",
            ref_image_paths=["/path/a.png", "/path/b.png"],
            ref_video_path="/path/clip.mp4",
            duration=10,
        )

        # 文生视频（不传任何参考媒体）+ 挂 LoRA
        run(prompt_text="...", duration=10, megapixels=0.6,
            mode="text_to_video", lora="synthpussy")
    """
    # 1. 上传素材
    if ref_image_paths:
        ref_images = [upload_image(p) for p in ref_image_paths]
    if ref_video_path:
        ref_video = upload_video(ref_video_path)
    if ref_audio_path:
        ref_audio = upload_audio(ref_audio_path)

    # 2. 构造 prompt
    prompt = build_prompt(
        prompt_text=prompt_text,
        ref_images=ref_images,
        ref_video=ref_video,
        ref_audio=ref_audio,
        aspect_ratio=aspect_ratio,
        megapixels=megapixels,
        duration=duration,
        steps=steps,
        noise_seed=noise_seed,
        workflow_id=workflow_id,
        mode=mode,
        lora=lora,
        lora_strength=lora_strength,
    )

    # 3. 提交
    prompt_id = submit_prompt(prompt)
    print(f"[提交成功] prompt_id = {prompt_id}")
    if on_submit:
        on_submit(prompt_id)

    # 4. 轮询
    entry = poll_history(prompt_id)
    print(f"[执行完成] status = success")

    # 5. 提取 + 下载（只保留视频产物为 saved，中间节点 png 只作 outputs 记录）
    outputs = extract_outputs(entry)
    print(f"[输出文件] {len(outputs)} 个")
    saved = []
    for out in outputs:
        fn = out.get("filename") or ""
        if fn.endswith((".mp4", ".webm", ".mov")):
            path = download_output(out, download_dir)
            saved.append(path)
            print(f"  [视频] 已下载: {path}")
        elif fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
            print(f"  [中间节点] 跳过: {fn}")   # 不落盘，避免污染产物目录
    return {"prompt_id": prompt_id, "outputs": outputs, "saved": saved}


# ==================== CLI ====================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="RunningHub 工作流 API 直调")
    ap.add_argument("--prompt", type=str, default=None, help="提示词")
    ap.add_argument("--image", action="append", default=[], help="本地图片路径（可多次）")
    ap.add_argument("--video", type=str, default=None, help="本地视频路径")
    ap.add_argument("--audio", type=str, default=None, help="本地音频路径")
    ap.add_argument("--ratio", type=str, default="9:16 (Portrait Widescreen)", help="分辨率比例")
    ap.add_argument("--megapixels", type=float, default=0.4, help="分辨率档位")
    ap.add_argument("--duration", type=float, default=5, help="时长（秒）")
    ap.add_argument("--steps", type=int, default=8, help="采样步数")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--out", type=str, default="outputs", help="下载目录")
    # 模式（不传参考媒体即 T2V）
    ap.add_argument("--mode", type=str, default=None,
                    choices=["text_to_video", "omni_reference", "first_last_frame"],
                    help="生成模式；不传则自动（有参考媒体 → omni_reference，无 → text_to_video）")
    # LoRA（串在 turbo 之后，新增不替换）
    ap.add_argument("--lora", type=str, default=None,
                    help="LoRA 预设名或文件名（如 synthpussy）；不传则不挂")
    ap.add_argument("--lora-strength", type=float, default=None,
                    help="LoRA 强度（默认用预设值，synthpussy 为 0.9）")
    args = ap.parse_args()

    run(
        prompt_text=args.prompt,
        ref_image_paths=args.image or None,
        ref_video_path=args.video,
        ref_audio_path=args.audio,
        aspect_ratio=args.ratio,
        megapixels=args.megapixels,
        duration=args.duration,
        steps=args.steps,
        noise_seed=args.seed,
        download_dir=args.out,
        mode=args.mode,
        lora=args.lora,
        lora_strength=args.lora_strength,
    )