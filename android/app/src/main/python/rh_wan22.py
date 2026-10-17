#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub · Wan Animate 2.2 动作迁移（平台托管版）
==================================================
工作流：2110720306189381634（平台托管，走 getJsonApiFormat API 实时拉取，
        不依赖本地 JSON 打包，安卓/本地行为一致）

技术路线：生成式角色替换
  - 参考图决定「身份」（脸/发型/服装）
  - 源视频决定「动作」（骨骼 + 人脸检测驱动）
  - ⚠️ 会连同服装/背景一起重画（区别于 ReActor 的像素级只换脸）

核心节点：WanVideoAnimateEmbeds(62) ← ClipVision(70) + 骨骼检测(211) + 人脸检测(213)
产物：默认只出 1 份主产物 [30]（带音轨）；compare=True 时额外保留对比条 [201]

相比作者原版的改动：
  1. [216] index 0→1：启用 face_images 人脸增强（作者默认走黑图关闭，实测开启后相似度更好）
  2. 默认删除 [201]（对比拼接条），只出一份；compare=True 才保留
  3. [112] 人脸检测预览节点 save_output=false，不产出，始终保留（不影响产物数）
  4. 分辨率：[184]=宽、[183]=高（已实证：VHS_LoadVideo.custom_width←184 / custom_height←183）
     ⚠️ 注意 183/184 与「数字大小顺序」直觉相反，改错会导致宽高颠倒
"""
import json
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""   # 由 webui 运行时注入
BASE = "https://www.runninghub.ai"
PROXY = f"{BASE}/proxy/{API_KEY}"

WORKFLOW_ID = "2110720306189381634"

# 节点 ID（来自平台 getJsonApiFormat）
NODE = {
    "ref_image": "57",     # LoadImage（参考图 / 身份）
    "ref_video": "63",     # VHS_LoadVideo（源视频 / 动作）
    "w": "184",            # JWInteger：宽（实证：custom_width←184）
    "h": "183",            # JWInteger：高（实证：custom_height←183）
    "resize": "64",        # ImageResizeKJv2
    "clipvis": "70",       # WanVideoClipVisionEncode
    "face_sw": "214",      # easy anythingIndexSwitch（人脸增强开关）
    "face_idx": "216",     # Primitive integer：0=黑图(关) / 1=真实人脸(开)
    "prompt": "190",       # CR Prompt Text
    "sampler": "27",       # WanVideoSampler
    "out_main": "30",      # VHS_VideoCombine：⚠️ 实为「产物+素材条」横向拼接版（[66] concat，宽会变大），带音轨
    "out_cmp": "201",      # VHS_VideoCombine：纯净产物（images←[28] 直连 decode），无音轨
    "out_face": "112",     # VHS_VideoCombine 人脸检测预览（save_output=false）
}

# 通用预置提示词（留空时自动使用）
# 目标：让参考图的人物「复刻示例视频里的服装 + 动作姿态」，不带入多余场景设定
DEFAULT_PROMPT = (
    "参考图中的人物，完整复刻示例视频里人物的服装、动作与姿态。"
    "保持与示例视频完全一致的穿着（款式、颜色、长短一致），"
    "并保持示例视频中的身体动作、手势、走位与镜头节奏一致；"
    "人物身份（脸、发型、体型）来自参考图，其余画面元素以示例视频为准。"
)

# 分辨率档位（宽 x 高）
RES_PRESETS = {
    "480x832": (480, 832),
    "544x960": (544, 960),
    "640x640": (640, 640),
    "832x480": (832, 480),
}


# ==================== 基础请求 ====================
def _request(url, method="GET", body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    if body is not None:
        req.add_header("content-type", "application/json")
    req.add_header("Authorization", API_KEY)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read().decode())


def _upload(path, field="image", ct=None):
    """multipart 上传，返回 ComfyUI 文件名。

    ⚠️ 实证（2026-10-15）：平台只有 /proxy/<key>/upload/image 一个上传端点，
       upload/video 返回 404 NOT_FOUND。视频也走这个端点，且字段名必须是 image。
       所以这里忽略传入的 field，统一用 image。
    """
    path = str(path)
    if ct is None:
        low = path.lower()
        if low.endswith(".mp4"):
            ct = "video/mp4"
        elif low.endswith(".png"):
            ct = "image/png"
        elif low.endswith(".webp"):
            ct = "image/webp"
        else:
            ct = "image/jpeg"
    field = "image"     # 平台只认这个字段名（video 会 400）
    data = Path(path).read_bytes()
    boundary = uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="{field}"; '
             f'filename="{Path(path).name}"\r\n').encode()
    body += f"Content-Type: {ct}\r\n\r\n".encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{PROXY}/upload/image", data=body, method="POST")
    req.add_header("accept", "application/json")
    req.add_header("content-type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", API_KEY)
    resp = urllib.request.urlopen(req, timeout=180)
    d = json.loads(resp.read().decode())
    # 错误码检查（如 809 APIKEY_FILE_SIZE_EXCEEDED）
    if isinstance(d, dict) and d.get("code") not in (None, 0, 200):
        raise RuntimeError(f"上传失败: code={d.get('code')} msg={d.get('msg')}")
    name = d.get("name") if isinstance(d, dict) else None
    if not name:
        raise RuntimeError(f"上传返回异常: {str(d)[:200]}")
    return name


def upload_image(p):
    return _upload(p, "image")


def upload_video(p):
    return _upload(p, "video", "video/mp4")


# ==================== 1. 拉取工作流（平台托管，实时） ====================
def get_workflow_prompt(workflow_id=WORKFLOW_ID) -> dict:
    """从平台拉取 API format（实时，不依赖本地 JSON 打包）"""
    d = _request(
        f"{BASE}/api/openapi/getJsonApiFormat",
        method="POST",
        body={"apiKey": API_KEY, "workflowId": str(workflow_id)},
        timeout=60,
    )
    if d.get("code") != 0:
        raise RuntimeError(f"getJsonApiFormat 失败: code={d.get('code')} msg={d.get('msg')}")
    p = d["data"]["prompt"]
    # 平台返回可能是 JSON 字符串，需二次解析
    if isinstance(p, str):
        p = json.loads(p)
    return p


# ==================== 2. 构造 prompt ====================
def build_prompt(
    prompt: dict,
    ref_image_fn: str,
    ref_video_fn: str,
    width: int = 480,
    height: int = 832,
    prompt_text: str = None,
    face_enhance: bool = True,
    compare: bool = False,
    seed: int = None,
    steps: int = None,
) -> dict:
    """在平台原始 prompt 上改参数。

    compare=False → 删除 [201] 对比条，只出 [30] 主产物（1 份）
    compare=True  → 保留 [201]，出 2 份
    """
    p = json.loads(json.dumps(prompt))   # 深拷贝，不污染缓存

    # --- 输入素材 ---
    p[NODE["ref_image"]]["inputs"]["image"] = ref_image_fn
    p[NODE["ref_video"]]["inputs"]["video"] = ref_video_fn

    # --- 分辨率（183=高, 184=宽） ---
    p[NODE["h"]]["inputs"]["value"] = int(height)
    p[NODE["w"]]["inputs"]["value"] = int(width)
    if NODE["resize"] in p:
        p[NODE["resize"]]["inputs"]["width"] = [NODE["w"], 0]
        p[NODE["resize"]]["inputs"]["height"] = [NODE["h"], 0]

    # --- 人脸增强开关：1=启用真实人脸检测，0=黑图（作者默认） ---
    p[NODE["face_idx"]]["inputs"]["int"] = 1 if face_enhance else 0

    # --- 提示词（留空则用通用预置词） ---
    if prompt_text and str(prompt_text).strip():
        p[NODE["prompt"]]["inputs"]["prompt"] = str(prompt_text).strip()
    elif DEFAULT_PROMPT:
        p[NODE["prompt"]]["inputs"]["prompt"] = DEFAULT_PROMPT

    # --- 采样（可选覆盖） ---
    if NODE["sampler"] in p:
        s = p[NODE["sampler"]]["inputs"]
        if seed is not None:
            s["seed"] = int(seed)
        if steps is not None:
            s["steps"] = int(steps)

    # --- 产物裁剪 ---
    # 实证（2026-10-15）：
    #   [30]  images ← [66] ImageConcatMulti(direction=left) = 「产物 + 素材条」横向拼接，宽度会变大（480→614）
    #   [201] images ← [28] 直连 decode = 纯净产物，尺寸严格等于设定分辨率
    # 所以：默认只出 [201]（纯净产物），compare=True 才额外保留 [30]（拼接对比版）。
    # [201] 默认无音轨，这里把 [63] 的音轨接上。
    if NODE["out_cmp"] in p:
        p[NODE["out_cmp"]]["inputs"].setdefault("audio", [NODE["ref_video"], 2])
    if not compare:
        p.pop(NODE["out_main"], None)     # 删掉拼接版，只留纯净产物

    return p


# ==================== 3. 提交 / 轮询 / 下载 ====================
def submit_prompt(prompt: dict, client_id: str = "") -> str:
    body = {"prompt": prompt, "client_id": client_id or uuid.uuid4().hex}
    d = _request(f"{PROXY}/prompt", method="POST", body=body, timeout=120)
    return d.get("prompt_id") or d.get("node_id") or d.get("id")


def poll_history(prompt_id, timeout=3600, interval=10, on_ping=None):
    """轮询直到完成/失败。返回 history entry。"""
    t0 = time.time()
    while True:
        try:
            d = _request(f"{PROXY}/history/{prompt_id}", timeout=60)
            entry = (d or {}).get(prompt_id) or (d or {}).get(str(prompt_id))
            if entry:
                st = (entry.get("status") or {}).get("status_str")
                if st in ("success", "error"):
                    return entry
                if entry.get("outputs"):
                    return entry
        except urllib.error.HTTPError:
            pass
        except Exception:
            pass
        if time.time() - t0 > timeout:
            raise TimeoutError(f"轮询超时 {timeout}s: {prompt_id}")
        if on_ping:
            try:
                on_ping(int(time.time() - t0))
            except Exception:
                pass
        time.sleep(interval)


def extract_outputs(entry) -> list:
    """从 history entry 提取产物（只取视频，跳过中间 png）"""
    out = []
    for _nid, node_out in (entry.get("outputs") or {}).items():
        for k in ("gifs", "videos", "images", "audio"):
            for item in (node_out.get(k) or []):
                fn = item.get("filename")
                if not fn:
                    continue
                if fn.lower().endswith((".mp4", ".webm", ".mov")):
                    out.append({
                        "filename": fn,
                        "objectKey": item.get("objectKey") or item.get("file_path") or fn,
                        "type": item.get("type", "output"),
                        "kind": "video",
                    })
    return out


def download_output(output, dest_dir="."):
    """下载产物到 dest_dir，返回本地路径"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(output["filename"]).name
    key = output.get("objectKey") or output["filename"]
    url = f"https://rh-hk-images.xiaoyaoyou.com/{key}" if not key.startswith("http") else key
    req = urllib.request.Request(url)
    req.add_header("Authorization", API_KEY)
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())
    return str(dest)


# ==================== 4. 一键跑 ====================
def run(
    ref_image_path: str,
    ref_video_path: str,
    width: int = 480,
    height: int = 832,
    prompt_text: str = None,
    face_enhance: bool = True,
    compare: bool = False,
    seed: int = None,
    steps: int = None,
    workflow_id: str = WORKFLOW_ID,
    download_dir: str = ".",
    on_submit=None,
    on_ping=None,
) -> dict:
    """完整流程：上传 → 拉工作流 → 改参 → 提交 → 轮询 → 下载"""
    print("=" * 60)
    print(f"[Wan Animate 2.2] 分辨率 {width}x{height} | 人脸增强={'开' if face_enhance else '关'} | 对比条={'开' if compare else '关'}")

    # 1. 上传
    img_fn = upload_image(ref_image_path)
    print(f"  参考图已上传: {img_fn}")
    vid_fn = upload_video(ref_video_path)
    print(f"  源视频已上传: {vid_fn}")

    # 2. 拉取工作流（平台实时）
    base = get_workflow_prompt(workflow_id)
    print(f"  工作流节点: {len(base)}")

    # 3. 构造
    p = build_prompt(base, img_fn, vid_fn, width=width, height=height,
                     prompt_text=prompt_text, face_enhance=face_enhance,
                     compare=compare, seed=seed, steps=steps)
    print(f"  提交节点: {len(p)}")

    # 4. 提交
    pid = submit_prompt(p)
    print(f"  已提交: prompt_id={pid}")
    if on_submit:
        try:
            on_submit(pid)
        except Exception:
            pass

    # 5. 轮询
    entry = poll_history(pid, on_ping=on_ping)
    st = (entry.get("status") or {}).get("status_str")
    if st == "error":
        msgs = (entry.get("status") or {}).get("messages") or []
        for m in msgs:
            if m and m[0] == "execution_error":
                raise RuntimeError(f"执行失败: {json.dumps(m[1], ensure_ascii=False)[:800]}")
        raise RuntimeError("执行失败（无详情）")
    print(f"  执行完成: {st}")

    # 6. 下载（只取视频）
    outputs = extract_outputs(entry)
    print(f"  产物: {len(outputs)} 个")
    saved = []
    for o in outputs:
        path = download_output(o, download_dir)
        saved.append(path)
        print(f"    ↓ {path}")
    return {"prompt_id": pid, "outputs": outputs, "videos": saved}


# ==================== CLI ====================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Wan Animate 2.2 动作迁移")
    ap.add_argument("--image", required=True, help="参考图（身份）")
    ap.add_argument("--video", required=True, help="源视频（动作）")
    ap.add_argument("--res", default="480x832", choices=list(RES_PRESETS), help="分辨率")
    ap.add_argument("--prompt", default=None, help="提示词（描述场景动作）")
    ap.add_argument("--no-face", action="store_true", help="关闭人脸增强")
    ap.add_argument("--compare", action="store_true", help="额外输出对比条")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--out", default="./out")
    a = ap.parse_args()
    w, h = RES_PRESETS[a.res]
    r = run(a.image, a.video, w, h, a.prompt,
            face_enhance=not a.no_face, compare=a.compare,
            seed=a.seed, steps=a.steps, download_dir=a.out)
    print("\n完成:", r["videos"])