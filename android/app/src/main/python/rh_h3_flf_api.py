#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub 首尾帧视频工作流 API 直调脚本
========================================
工作流：2108688116525977602「minimaxH3首尾帧视频工作流（超级加速版）」
核心节点：MiniMaxH3ImageToVideo (133)，first_frame(114) + last_frame(144)

走 Native ComfyUI 代理端点 `/proxy/<api-key>`：
  POST /upload/image        上传首/尾帧 → 文件名
  POST /prompt              提交完整 prompt → prompt_id
  GET  /history/<prompt_id> 轮询状态 → success
  GET  https://rh-hk-images.xiaoyou.com/<objectKey>  下载 mp4

功能：
  1. 单段生成：首帧图（+可选尾帧）→ 视频
  2. 续接生成：抽上一段尾帧 → 作为下一段首帧 → 多段串联（只出分段，不拼接）

拼接请用独立工具 `rh_video_merge.py`（同目录）：
  先人工检查各段效果，满意后再合并，避免把不满意的段落也拼进去。

已验证（2026-10-10）：
- 只传首帧（删 last_frame）后端认，I2V 模式 ✅
- 抽尾帧续接：画面姿势/服装/场景/人物位置高度对齐 ✅
- concat 拼接：两段 h264+aac 32kHz 参数一致，-c copy 秒拼 ✅（见 rh_video_merge.py）
"""

import json
import time
import uuid
import subprocess
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
WORKFLOW_ID = "2108688116525977602"

# 节点 ID 映射（来自 getJsonApiFormat）
NODE = {
    "main": "133",       # MiniMaxH3ImageToVideo
    "first": "114",      # LoadImage 首帧
    "last": "144",       # LoadImage 尾帧
    "prompt": "155",     # PrimitiveStringMultiline（提示词）
    "duration": "135",   # PrimitiveFloat（时长秒）
    "length": "134",     # ComfyMathExpression（秒 → 帧数，自动对齐 17 倍数）
    "resolution": "115", # ResolutionSelector
    "noise": "131",      # RandomNoise
    "steps": "126",      # BasicScheduler（8 步 turbo）
    "save": "92",        # SaveVideo
}

DEFAULT_RATIO = "9:16 (Portrait Widescreen)"
DEFAULT_MEGAPIXELS = 0.6
DEFAULT_DURATION = 5
DEFAULT_STEPS = 8


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


def upload_image(file_path: str) -> str:
    """上传图片，返回 ComfyUI 文件名"""
    ct = "image/png" if str(file_path).lower().endswith(".png") else "image/jpeg"
    d = _upload_multipart(f"{PROXY}/upload/image", file_path, "image", ct)
    return d["name"]


# ==================== 拉取并构造 prompt ====================
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
    first_frame: str = None,     # 首帧图片文件名
    last_frame: str = None,      # 尾帧图片文件名（可选）
    prompt_text: str = None,     # 提示词
    aspect_ratio: str = DEFAULT_RATIO,
    megapixels: float = DEFAULT_MEGAPIXELS,
    duration: float = DEFAULT_DURATION,
    steps: int = DEFAULT_STEPS,
    noise_seed: int = None,
    workflow_id: str = WORKFLOW_ID,
    lora: str = None,            # LoRA 预设名或文件名，None = 不挂
    lora_strength: float = None, # 强度；None = 用预设默认（synthpussy 为 0.9）
 ) -> dict:
    """
    构造完整可提交的 prompt

    首尾帧规则：
      - 只传 first_frame：删掉 last_frame 输入 + 节点 144（I2V 首帧模式）
      - 只传 last_frame：删掉 first_frame 输入 + 节点 114（尾帧模式）
      - 两个都传：首尾帧过渡模式

    LoRA：串在 turbo 8step LoRA 之后（新增，不替换）。详见 rh_h3_lora.py。
    """
    p = get_workflow_prompt(workflow_id)

    # ---- 首帧 ----
    if first_frame:
        p[NODE["first"]]["inputs"]["image"] = first_frame
    else:
        p[NODE["main"]]["inputs"].pop("first_frame", None)
        p.pop(NODE["first"], None)

    # ---- 尾帧 ----
    if last_frame:
        p[NODE["last"]]["inputs"]["image"] = last_frame
    else:
        p[NODE["main"]]["inputs"].pop("last_frame", None)
        p.pop(NODE["last"], None)

    # ---- 提示词 ----
    if prompt_text is not None:
        p[NODE["prompt"]]["inputs"]["value"] = prompt_text

    # ---- 分辨率 ----
    p[NODE["resolution"]]["inputs"]["aspect_ratio"] = aspect_ratio
    p[NODE["resolution"]]["inputs"]["megapixels"] = megapixels

    # ---- 时长（秒 → 帧数自动换算）----
    p[NODE["duration"]]["inputs"]["value"] = duration

    # ---- 步数 ----
    p[NODE["steps"]]["inputs"]["steps"] = steps

    # ---- 种子 ----
    if noise_seed is not None:
        p[NODE["noise"]]["inputs"]["noise_seed"] = noise_seed

    # ---- LoRA（串在 turbo 之后，新增不替换）----
    if lora:
        if apply_lora is None:
            raise RuntimeError("rh_h3_lora.py 不可用（需与本脚本同目录）")
        name, default_strength = resolve_lora(lora)
        strength = lora_strength if lora_strength is not None else default_strength
        p, _ = apply_lora(p, name, strength, workflow_id)

    return p


# ==================== 提交 / 轮询 / 下载 ====================
def submit_prompt(prompt: dict, client_id: str = "rh-h3-flf-script") -> str:
    """提交到 Native ComfyUI /prompt，返回 prompt_id"""
    d = _request(f"{PROXY}/prompt", method="POST",
                 body={"prompt": prompt, "client_id": client_id}, timeout=120)
    if "prompt_id" in d:
        return d["prompt_id"]
    if d == {}:
        q = _request(f"{PROXY}/queue", timeout=30)
        running = q.get("queue_running", [])
        pending = q.get("queue_pending", [])
        if running:
            return str(running[-1][0])
        if pending:
            return str(pending[0][0])
    raise RuntimeError(f"submit failed: {d}")


def poll_history(prompt_id: str, interval: int = 15, max_wait: int = 900) -> dict:
    """轮询 /history/<prompt_id> 直到 success/fail"""
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
    raise RuntimeError("poll timeout")


def extract_outputs(entry: dict) -> list:
    """从 history entry 提取输出文件列表"""
    files = []
    for nid, out in entry.get("outputs", {}).items():
        for key in ("videos", "gifs", "images"):
            for f in out.get(key, []):
                files.append({
                    "node": nid,
                    "filename": f.get("filename"),
                    "objectKey": f.get("objectKey"),
                    "type": f.get("type"),
                })
    return files


def download_output(output: dict, dest_dir: str = ".") -> str:
    """下载输出文件到本地"""
    object_key = output["objectKey"]
    url = f"https://rh-hk-images.xiaoyaoyou.com/{object_key}"
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(dest_dir) / output["filename"]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())
    return str(dest)


# ==================== 视频处理（抽尾帧）====================
def extract_tail_frame(video_path: str, out_png: str) -> str:
    """抽视频最后一帧，返回 png 路径"""
    subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-0.1", "-i", video_path,
         "-frames:v", "1", "-q:v", "2", out_png],
        capture_output=True, text=True,
    )
    if not Path(out_png).exists():
        raise RuntimeError(f"抽尾帧失败: {video_path}")
    return out_png


# ==================== 一站式：单段 ====================
def run(
    prompt_text: str = None,
    first_frame_path: str = None,   # 本地首帧图路径（自动上传）
    last_frame_path: str = None,    # 本地尾帧图路径（自动上传）
    first_frame: str = None,        # 或直接传已上传文件名
    last_frame: str = None,
    aspect_ratio: str = DEFAULT_RATIO,
    megapixels: float = DEFAULT_MEGAPIXELS,
    duration: float = DEFAULT_DURATION,
    steps: int = DEFAULT_STEPS,
    noise_seed: int = None,
    download_dir: str = "outputs",
    workflow_id: str = WORKFLOW_ID,
    lora: str = None,
    lora_strength: float = None,
 ) -> dict:
    """单段：上传 → 构造 → 提交 → 轮询 → 下载，返回 {prompt_id, video, ...}"""
    if first_frame_path:
        first_frame = upload_image(first_frame_path)
        print(f"[首帧] {Path(first_frame_path).name} → {first_frame}")
    if last_frame_path:
        last_frame = upload_image(last_frame_path)
        print(f"[尾帧] {Path(last_frame_path).name} → {last_frame}")

    prompt = build_prompt(
        first_frame=first_frame, last_frame=last_frame, prompt_text=prompt_text,
        aspect_ratio=aspect_ratio, megapixels=megapixels, duration=duration,
        steps=steps, noise_seed=noise_seed, workflow_id=workflow_id,
        lora=lora, lora_strength=lora_strength,
    )
    print(f"[模式] {'首尾帧' if first_frame and last_frame else ('首帧' if first_frame else '尾帧')} "
          f"| {aspect_ratio} | mp={megapixels} | {duration}s | steps={steps}")

    prompt_id = submit_prompt(prompt)
    print(f"[提交成功] prompt_id = {prompt_id}")

    entry = poll_history(prompt_id)
    print("[执行完成] status = success")

    outputs = extract_outputs(entry)
    saved = []
    for out in outputs:
        if out["filename"] and out["filename"].endswith((".mp4", ".webm")):
            path = download_output(out, download_dir)
            saved.append(path)
            print(f"  已下载: {path}")
    return {"prompt_id": prompt_id, "outputs": outputs, "videos": saved,
            "first_frame": first_frame, "last_frame": last_frame}


# ==================== 一站式：续接 ====================
def run_continue(
    prompt_text: str,
    first_frame_path: str,
    next_prompts: list,             # 续接段提示词列表，长度 = 续接段数
    n_segments: int = None,         # 总段数（含第一段）；默认 = 1 + len(next_prompts)
    aspect_ratio: str = DEFAULT_RATIO,
    megapixels: float = DEFAULT_MEGAPIXELS,
    duration: float = DEFAULT_DURATION,
    steps: int = DEFAULT_STEPS,
    noise_seed: int = None,
    download_dir: str = "outputs",
    workflow_id: str = WORKFLOW_ID,
    lora: str = None,
    lora_strength: float = None,
 ) -> dict:
    """
    续接生成：第 1 段用 first_frame_path，之后每段自动抽上一段尾帧作首帧。
    只出分段视频，不拼接——拼接请用独立的 `rh_video_merge.py` 工具
    （先人工检查各段效果，满意后再拼，避免把不满意的段落也拼进去）。

    next_prompts: 第 2..N 段的提示词；若不足则循环用最后一条。
    返回 {"segments": [...], "videos": [...]}，各段视频按顺序排列。
    """
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    total = n_segments or (1 + len(next_prompts))
    cur_first_path = first_frame_path
    seg_videos = []
    seg_records = []

    for i in range(total):
        if i == 0:
            ptext = prompt_text
        else:
            idx = i - 1
            ptext = next_prompts[idx] if idx < len(next_prompts) else (next_prompts[-1] if next_prompts else prompt_text)

        print(f"\n===== 第 {i + 1}/{total} 段 =====")
        res = run(
            prompt_text=ptext,
            first_frame_path=cur_first_path,
            aspect_ratio=aspect_ratio, megapixels=megapixels,
            duration=duration, steps=steps,
            noise_seed=(noise_seed + i) if noise_seed is not None else None,
            download_dir=download_dir, workflow_id=workflow_id,
            lora=lora, lora_strength=lora_strength,
        )
        if not res["videos"]:
            raise RuntimeError(f"第 {i + 1} 段没有输出视频")
        seg_video = res["videos"][0]
        seg_videos.append(seg_video)
        seg_records.append({"seg": i + 1, "prompt_id": res["prompt_id"], "video": seg_video})

        # 抽尾帧 → 下一段首帧
        if i < total - 1:
            tail_png = str(Path(download_dir) / f"tail_seg{i + 1}.png")
            extract_tail_frame(seg_video, tail_png)
            print(f"[尾帧续接] 第 {i + 1} 段尾帧 → {tail_png}")
            cur_first_path = tail_png

    print(f"\n[完成] 共 {len(seg_videos)} 段，未拼接。满意后可用拼接工具合并：")
    print(f"  python rh_video_merge.py {' '.join(seg_videos)} -o {Path(download_dir) / 'merged_full.mp4'}")
    return {"segments": seg_records, "videos": seg_videos}


# ==================== CLI ====================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="RunningHub 首尾帧视频工作流 API 直调")
    ap.add_argument("--prompt", type=str, default=None, help="提示词（首段）")
    ap.add_argument("--first", type=str, default=None, help="本地首帧图路径")
    ap.add_argument("--last", type=str, default=None, help="本地尾帧图路径（可选）")
    ap.add_argument("--ratio", type=str, default=DEFAULT_RATIO, help="分辨率比例")
    ap.add_argument("--megapixels", type=float, default=DEFAULT_MEGAPIXELS, help="分辨率档位")
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION, help="每段时长（秒）")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS, help="采样步数")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--out", type=str, default="outputs", help="下载目录")
    # LoRA（串在 turbo 之后，新增不替换）
    ap.add_argument("--lora", type=str, default=None,
                    help="LoRA 预设名或文件名（如 synthpussy）；不传则不挂")
    ap.add_argument("--lora-strength", type=float, default=None,
                    help="LoRA 强度（默认用预设值，synthpussy 为 0.9）")
    # 续接
    ap.add_argument("--continue", dest="cont", type=int, default=0,
                    help="续接段数（用上一段尾帧作下一段首帧）")
    ap.add_argument("--next-prompt", action="append", default=[],
                    help="续接段提示词（可多次，按顺序对应各续接段）")
    args = ap.parse_args()

    if args.cont > 0:
        run_continue(
            prompt_text=args.prompt,
            first_frame_path=args.first,
            next_prompts=args.next_prompt,
            n_segments=1 + args.cont,
            aspect_ratio=args.ratio, megapixels=args.megapixels,
            duration=args.duration, steps=args.steps,
            noise_seed=args.seed, download_dir=args.out,
            lora=args.lora, lora_strength=args.lora_strength,
        )
    else:
        run(
            prompt_text=args.prompt,
            first_frame_path=args.first,
            last_frame_path=args.last,
            aspect_ratio=args.ratio, megapixels=args.megapixels,
            duration=args.duration, steps=args.steps,
            noise_seed=args.seed, download_dir=args.out,
            lora=args.lora, lora_strength=args.lora_strength,
        )