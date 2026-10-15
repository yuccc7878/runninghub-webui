#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub MiniMaxH3 视频换脸脚本（V2V Face Swap）
========================================
由 rh_workflow_api.py（omni 参考）复制改造。

输入：
  - 参考人脸图（ref_image_paths，1 张即可，越多越稳）
  - 参考视频（ref_video_path，要换脸的目标视频）
输出：
  - H3 主模以"人像参考 + 视频参考"驱动，生成同视频内容/动作但人脸被替换的新视频

关键差异 vs rh_workflow_api.py：
  1. 强制 `omni_reference` 模式（人脸图 + 视频双参考）
  2. 【所有自定义 LoRA 全部断掉】只保留工作流自带的 turbo 8step 加速 LoRA
     （turbo 是 steps=8 必需，摘了会严重欠采样画面糊 —— 见 rh_h3_lora.py 实测）
  3. 移除了 lora / lora_strength 参数（永不挂自定义 LoRA）

工作流：2108502955011149825（minimaxH3超级加速全能工作流）
核心节点：MiniMaxH3Unified (160) omni_reference 模式
人脸 → OAILoadImage (251+)，视频 → OAILoadVideo (256)
"""

import json
import time
import uuid
import urllib.request
from pathlib import Path

# 不 import rh_h3_lora —— 本脚本不使用任何自定义 LoRA

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
    "ref_audio": "257",     # OAILoadAudio（本脚本不使用）
    "prompt": "144",        # CR Text（提示词）
    "resolution": "115",    # ResolutionSelector
    "noise": "129",         # RandomNoise
    "steps": "204",         # BasicScheduler（8 步 turbo）
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
    """上传视频，返回文件名（ComfyUI /upload/image 靠 filename 后缀识别类型，字段固定 image）"""
    ct = "video/mp4"
    d = _upload_multipart(f"{PROXY}/upload/image", file_path, "image", ct)
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
    ref_images: list = None,      # 人脸参考图文件名列表（1~5 张）
    ref_video: str = None,        # 目标视频文件名（必填，要换脸的视频）
    aspect_ratio: str = "9:16 (Portrait Widescreen)",
    megapixels: float = 0.4,
    duration: float = 5,
    steps: int = 8,
    noise_seed: int = None,
    workflow_id: str = WORKFLOW_ID,
) -> dict:
    """
    构造视频换脸 prompt（强制 omni_reference 模式）

    输入:
        prompt_text: 提示词（描述换脸后的期望效果/画面）
        ref_images:  参考人脸图文件名（OAILoadImage，1~5 张）
        ref_video:   目标视频文件名（OAILoadVideo，必填）

    LoRA：本脚本不使用任何自定义 LoRA，只保留工作流自带的
          turbo 8step 加速 LoRA（steps=8 必需，摘了会严重欠采样糊画面）。
    """
    p = get_workflow_prompt(workflow_id)

    # ---- 强制 omni_reference 模式（人脸图 + 视频双参考）----
    p[NODE["main"]]["inputs"]["mode"] = "omni_reference"

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

    # 步数（turbo 8 步）
    p[NODE["steps"]]["inputs"]["steps"] = steps

    # ---- 先清旧动态孔键 ----
    for k in list(p[NODE["main"]]["inputs"].keys()):
        if k.startswith("mode.ref_images.") or k.startswith("mode.ref_videos.") \
           or k.startswith("mode.ref_video_audios.") or k.startswith("mode.ref_audios."):
            del p[NODE["main"]]["inputs"][k]

    # ---- 人脸参考图（动态孔连线）----
    if ref_images:
        for i, img in enumerate(ref_images[:5]):
            node_id = str(251 + i)   # 251~255
            p[node_id]["inputs"]["image"] = img
            p[NODE["main"]]["inputs"][f"mode.ref_images.ref_image_{i}"] = [node_id, 0]

    # ---- 参考视频（必填，保持动作/内容）----
    # 只连 ref_videos（纯视频参考），不连 ref_video_audios（音视频配对孔在该工作流
    # 会触发 _prepare_audio_slots 加载音频失败 KeyError 'path'，实测 2026-10-14）
    if not ref_video:
        raise RuntimeError("视频换脸必须提供参考视频（ref_video）")
    p[NODE["ref_video"]]["inputs"]["video"] = ref_video
    p[NODE["main"]]["inputs"]["mode.ref_videos.ref_video_0"] = [NODE["ref_video"], 0]

    # 本脚本不挂任何自定义 LoRA（turbo 工作流自带，保留不动）

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
    ref_image_paths: list = None,   # 本地人脸图路径（自动上传）
    ref_video_path: str = None,     # 本地视频路径（自动上传，必填）
    aspect_ratio: str = "9:16 (Portrait Widescreen)",
    megapixels: float = 0.4,
    duration: float = 5,
    steps: int = 8,
    noise_seed: int = None,
    download_dir: str = "outputs",
    workflow_id: str = WORKFLOW_ID,
    on_submit: callable = None,     # 提交成功回调 on_submit(prompt_id)，用于立即持久化 pid
):
    """
    视频换脸一键执行：上传素材 → 构造 prompt → 提交 → 轮询 → 下载

    示例:
        # 用 face.png 的人脸，把 clip.mp4 里的人脸换掉
        run(
            prompt_text="保持原视频动作与内容，把人物脸部替换为参考人脸",
            ref_image_paths=["/path/face.png"],
            ref_video_path="/path/clip.mp4",
            duration=10,
        )
    """
    # 1. 上传素材（人脸图可多个，视频必填）
    if ref_image_paths:
        ref_images = [upload_image(p) for p in ref_image_paths]
    if not ref_video_path:
        raise RuntimeError("视频换脸必须提供参考视频路径（ref_video_path）")
    ref_video = upload_video(ref_video_path)

    # 2. 构造 prompt
    prompt = build_prompt(
        prompt_text=prompt_text,
        ref_images=ref_images,
        ref_video=ref_video,
        aspect_ratio=aspect_ratio,
        megapixels=megapixels,
        duration=duration,
        steps=steps,
        noise_seed=noise_seed,
        workflow_id=workflow_id,
    )

    # 3. 提交
    prompt_id = submit_prompt(prompt)
    print(f"[提交成功] prompt_id = {prompt_id}")
    if on_submit:
        on_submit(prompt_id)

    # 4. 轮询
    entry = poll_history(prompt_id)
    print(f"[执行完成] status = success")

    # 5. 提取 + 下载（只保留视频产物为 videos，中间节点 png 只作 outputs 记录）
    outputs = extract_outputs(entry)
    print(f"[输出文件] {len(outputs)} 个")
    saved = []
    videos = []
    for out in outputs:
        fn = out.get("filename") or ""
        if fn.endswith((".mp4", ".webm", ".mov")):
            path = download_output(out, download_dir)
            saved.append(path)
            videos.append(path)
            print(f"  [视频] 已下载: {path}")
        elif fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
            print(f"  [中间节点] 跳过: {fn}")   # 不落盘，避免污染产物目录
    return {"prompt_id": prompt_id, "outputs": outputs, "videos": videos, "saved": videos}


# ==================== CLI ====================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="RunningHub MiniMaxH3 视频换脸")
    ap.add_argument("--prompt", type=str, default=None, help="提示词（描述期望效果）")
    ap.add_argument("--image", action="append", default=[], help="参考人脸图路径（可多次，1~5 张）")
    ap.add_argument("--video", type=str, default=None, help="目标视频路径（必填，要换脸的视频）")
    ap.add_argument("--ratio", type=str, default="9:16 (Portrait Widescreen)", help="分辨率比例")
    ap.add_argument("--megapixels", type=float, default=0.4, help="分辨率档位")
    ap.add_argument("--duration", type=float, default=5, help="时长（秒）")
    ap.add_argument("--steps", type=int, default=8, help="采样步数（turbo 8 步）")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--out", type=str, default="outputs", help="下载目录")
    args = ap.parse_args()

    run(
        prompt_text=args.prompt,
        ref_image_paths=args.image or None,
        ref_video_path=args.video,
        aspect_ratio=args.ratio,
        megapixels=args.megapixels,
        duration=args.duration,
        steps=args.steps,
        noise_seed=args.seed,
        download_dir=args.out,
    )