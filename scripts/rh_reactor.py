#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ReActor 视频换脸（RunningHub 平台托管工作流）

与 wan22_animate 的区别：
  - wan22 是「生成式动作迁移」：参考图给身份 + 源视频给动作，整体重画（会改服装/背景）
  - ReActor 是「像素级换脸」：只替换源视频里的人脸，身体/动作/服装/背景原样保留

工作流：workflowId 2110722883786321922（14 节点，平台实时拉取）
链路：
  参考脸[10] ─────────────────────────────→ source_image[15]
  源视频[11] ─┬→ ImageResizeKJ[14] ───────→ input_image[15]
              ├→ VHS_VideoInfo[19] → SimpleMath[22] → fps[18][21]
              └→ audio[18][21]
  ReActorFaceSwap[15] ┬→ RIFE插帧[26] → VideoCombine[18]   ← 纯净产物（默认）
                      └→ ImageConcanate[20] → [21]         ← 对比版（compare 开关）
"""
import json
import os
import time
import urllib.request
import urllib.error

BASE = "https://www.runninghub.ai"
WORKFLOW_ID = "2110722883786321922"

# ==================== 节点 ID 映射 ====================
NODE = {
    "source_face": "10",     # LoadImage：参考脸（提供要换上去的脸）
    "src_video": "11",       # VHS_LoadVideo：源视频（被换脸的视频）
    "interp": "13",          # CR Value：RIFE 插帧倍率
    "resize": "14",          # ImageResizeKJ：处理分辨率
    "reactor": "15",         # ReActorFaceSwap
    "out_main": "18",        # VideoCombine：纯净产物（带音轨）
    "out_cmp": "21",         # VideoCombine：对比版（右拼源视频）
    "rife": "26",            # RIFE VFI 插帧
}

# ReActor 是逐帧换脸，输出必须贴合源视频分辨率（不能像 wan22 那样套任意框）。
# 策略：读取源视频实际宽高 → 等比缩放到 max_side（防 OOM）→ 传给 ImageResizeKJ。
# 目标框比例 = 源视频比例，配合 keep_proportion=true，输出严格保持源视频宽高比。
MAX_SIDE_DEFAULT = 1024
MAX_SIDE_OPTIONS = [512, 768, 1024, 1280]


def probe_video(path):
    """读视频宽高（ffprobe）。失败返回 (0, 0)"""
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


def fit_resolution(src_w, src_h, max_side=MAX_SIDE_DEFAULT):
    """等比缩放到 max_side 以内，保持源比例。
    返回 (w, h)，不做 64 对齐——ImageResizeKJ 的 divisible_by=64 会自己处理，
    让目标框比例尽可能贴近源视频。"""
    if src_w <= 0 or src_h <= 0:
        return max_side, max_side
    scale = min(1.0, float(max_side) / max(src_w, src_h))
    w = max(64, int(round(src_w * scale)))
    h = max(64, int(round(src_h * scale)))
    return w, h


# RIFE 插帧倍率：1=不插帧，2=帧数翻倍（更顺滑，耗时更长）
INTERP_PRESETS = {"1": 1, "2": 2, "4": 4}

API_KEY = None


def _req(method, path, body=None, timeout=60, raw_key=None):
    """调平台接口。

    ⚠️ 响应格式不统一（实证）：
      - /api/openapi/getJsonApiFormat → {"code":0, "data":...}
      - /proxy/<key>/prompt           → {"prompt_id":"..."}      ← 没有 code
      - /proxy/<key>/history/<id>     → {"history":{...}}        ← 没有 code
      - /proxy/<key>/upload/image     → {"name":"..."}           ← 没有 code
    所以只在 code 存在且非 0/200 时才判错，不能一律要求 code==0。
    """
    key = raw_key or API_KEY
    if not key:
        raise RuntimeError("未设置 API Key")
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    if body is not None:
        req.data = json.dumps(body).encode()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("msg") or str(e.code)
        except Exception:
            msg = f"HTTP {e.code}"
        raise RuntimeError(msg)
    if isinstance(r, dict) and r.get("code") not in (None, 0, 200):
        raise RuntimeError(f"RunningHub 错误 {r.get('code')}: {r.get('msg') or r.get('errorMessage')}")
    return r


def fetch_prompt(api_key=None, workflow_id=WORKFLOW_ID):
    """从平台实时拉取工作流 prompt（不依赖本地打包 JSON）"""
    r = _req("POST", "/api/openapi/getJsonApiFormat",
             {"workflowId": workflow_id, "apiKey": api_key or API_KEY}, raw_key=api_key)
    d = r.get("data") if isinstance(r, dict) else r
    p = None
    if isinstance(d, dict):
        p = d.get("prompt")
    elif isinstance(d, str):
        p = d
    if isinstance(p, str):
        p = json.loads(p)
    if not isinstance(p, dict):
        raise RuntimeError("getJsonApiFormat 返回结构异常: " + str(r)[:300])
    return p


def submit(prompt, api_key=None, timeout=60):
    """提交任务，返回 prompt_id。响应是 {"prompt_id":"..."}（无 code 字段）"""
    r = _req("POST", f"/proxy/{api_key or API_KEY}/prompt",
             {"prompt": prompt, "workflowId": WORKFLOW_ID}, timeout=timeout, raw_key=api_key)
    pid = (r or {}).get("promptId") or (r or {}).get("prompt_id")
    if not pid:
        raise RuntimeError("提交未返回 prompt_id: " + str(r)[:300])
    return pid


def upload(file_path, api_key=None, timeout=180):
    """上传本地文件到平台，返回平台文件名。

    ⚠️ 实证（沿用 rh_wan22 的结论）：平台只有 /proxy/<key>/upload/image 一个端点，
    upload/video 返回 404 NOT_FOUND；视频也走它，且表单字段名必须是 image。
    成功响应是 {"name": "...", "subfolder": "", "type": "input"}（**没有 code 字段**），
    失败时才带 code/msg（如 809 APIKEY_FILE_SIZE_EXCEEDED）。
    """
    key = api_key or API_KEY
    if not key:
        raise RuntimeError("未设置 API Key")
    path = str(file_path)
    low = path.lower()
    if low.endswith(".mp4"):
        ct = "video/mp4"
    elif low.endswith(".webm"):
        ct = "video/webm"
    elif low.endswith(".mov"):
        ct = "video/quicktime"
    elif low.endswith(".png"):
        ct = "image/png"
    elif low.endswith(".webp"):
        ct = "image/webp"
    else:
        ct = "image/jpeg"

    import uuid
    from pathlib import Path as _P
    data = _P(path).read_bytes()
    boundary = uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="image"; '
             f'filename="{_P(path).name}"\r\n').encode()
    body += f"Content-Type: {ct}\r\n\r\n".encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(f"{BASE}/proxy/{key}/upload/image",
                                 data=body, method="POST")
    req.add_header("accept", "application/json")
    req.add_header("content-type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", key)
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))
    # 错误码检查（失败时才有 code，如 809 文件超限）
    if isinstance(d, dict) and d.get("code") not in (None, 0, 200):
        raise RuntimeError(f"上传失败: code={d.get('code')} msg={d.get('msg')}")
    name = d.get("name") if isinstance(d, dict) else None
    if not name:
        raise RuntimeError(f"上传返回异常: {str(d)[:200]}")
    return name


def build_prompt(base_prompt, source_face=None, src_video=None,
                 resolution=None, interp=2, compare=False,
                 face_restore=0.4, swap_model=None, facedetection=None,
                 frame_load_cap=1500):
    """在平台拉取的 prompt 基础上改写参数。

    source_face / src_video 是已上传到平台的文件名（objectKey）。
    resolution 为 None 时不改 resize 节点（保持工作流默认，或由 run() 已算好目标框后传入）。
    frame_load_cap：覆盖工作流写死的 90 帧上限（否则 >3.7s 的视频会被截断）。
    """
    p = json.loads(json.dumps(base_prompt))          # 深拷贝，不污染原始

    # --- 参考脸 / 源视频 ---
    if source_face:
        p[NODE["source_face"]]["inputs"]["image"] = source_face
    if src_video:
        p[NODE["src_video"]]["inputs"]["video"] = src_video

    # --- 关键：解除 VHS_LoadVideo 的 90 帧上限（实测 30s/720帧 源被截成 90 帧 → 产物 15s）---
    p[NODE["src_video"]]["inputs"]["frame_load_cap"] = int(frame_load_cap)

    # --- 关键：保持原时长（输出 fps = 源 fps）---
    # 工作流默认：SimpleMath[22] = 源fps / CRValue(2)，配合 select_every_nth=2 抽帧 + RIFE×2 补帧，
    # 帧数被补回 ≈源帧数，但 fps 砍半 → 30s 源变成 60s 慢动作产物（实测 T939: 719帧@12fps）。
    # 修复：VideoCombine 直接用 VHS_VideoInfo[19,0]（源帧率）封装，时长恢复 = 源时长。
    for _nid in (NODE["out_main"], NODE["out_cmp"]):
        if _nid in p:
            p[_nid]["inputs"]["frame_rate"] = ["19", 0]

    # --- 处理分辨率 ---
    if resolution:
        p[NODE["resize"]]["inputs"]["width"] = int(resolution[0])
        p[NODE["resize"]]["inputs"]["height"] = int(resolution[1])

    # --- RIFE 插帧倍率（CR Value）---
    if interp and str(interp) in INTERP_PRESETS:
        p[NODE["interp"]]["inputs"]["value"] = int(interp)

    # --- ReActor 可选覆盖 ---
    r = p.get(NODE["reactor"], {}).get("inputs", {})
    if face_restore is not None:
        r["face_restore_visibility"] = float(face_restore)
    if swap_model:
        r["swap_model"] = swap_model
    if facedetection:
        r["facedetection"] = facedetection

    # --- 产物裁剪：默认只出纯净版 [18]，compare=True 才额外保留对比版 [21] ---
    if not compare:
        p.pop(NODE["out_cmp"], None)

    return p




def history(prompt_id, api_key=None, timeout=60):
    return _req("GET", f"/proxy/{api_key or API_KEY}/history/{prompt_id}",
                timeout=timeout, raw_key=api_key)


def extract_outputs(entry):
    """从 history entry 提取产物列表。

    实测结构（对齐 rh_wan22）：
      history 响应 = {prompt_id: {outputs: {node_id: {videos|gifs|images|audio: [...]}}, status: {...}}}
    产物可能挂在 images 数组下但文件名是 .mp4（ReActor 实测如此），所以按文件名过滤。
    """
    files = []
    if not isinstance(entry, dict):
        return files
    for nid, out in (entry.get("outputs") or {}).items():
        for key in ("videos", "gifs", "images", "audio"):
            for f in (out.get(key) or []):
                fn = f.get("filename")
                if not fn:
                    continue
                files.append({"node": nid, "filename": fn,
                              "objectKey": f.get("objectKey") or f.get("file_path") or fn,
                              "type": f.get("type", "output")})
    return files


def download_output(out, download_dir="outputs"):
    """下载单个产物，返回本地路径"""
    obj = out.get("objectKey")
    fname = out.get("filename")
    if not obj or not fname:
        return None
    os.makedirs(download_dir, exist_ok=True)
    url = f"https://rh-hk-images.xiaoyaoyou.com/{obj}"
    dest = os.path.join(download_dir, fname)
    urllib.request.urlretrieve(url, dest)
    return dest


def poll_history(prompt_id, api_key=None, max_wait=1800, interval=10):
    """轮询到完成，返回 history entry。

    响应结构 = {prompt_id: {outputs:..., status: {status_str:...}}}，
    取 d.get(prompt_id) 得到 entry（对齐 rh_wan22 的解析）。
    """
    key = api_key or API_KEY
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            d = history(prompt_id, api_key=key)
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
        if time.time() - t0 > max_wait:
            break
        time.sleep(interval)
    raise RuntimeError(f"轮询超时 {max_wait}s: {prompt_id}")


def run(source_face=None, src_video=None, max_side=MAX_SIDE_DEFAULT,
        interp=2, compare=False, face_restore=0.4,
        download_dir="outputs", api_key=None, on_submit=None,
        no_wait=False):
    """一站式：拉工作流 → 探测源分辨率 → 上传 → 改写 → 提交 → 轮询 → 下载

    max_side：源视频最长边上限（等比缩放，保持源比例，防 OOM）。
    """
    key = api_key or API_KEY
    if not key:
        raise RuntimeError("未设置 API Key（请在设置里配置 RunningHub Key）")
    base = fetch_prompt(api_key=key)

    # 先探测源视频分辨率 → 算目标框（贴合源比例）
    target = None
    if src_video:
        sw, sh = probe_video(src_video)
        if sw > 0 and sh > 0:
            target = fit_resolution(sw, sh, max_side=int(max_side))
        else:
            target = None

    up_face = upload(source_face, api_key=key) if source_face else None
    up_vid = upload(src_video, api_key=key) if src_video else None

    p = build_prompt(base, source_face=up_face, src_video=up_vid,
                     resolution=target, interp=interp, compare=compare,
                     face_restore=face_restore)
    pid = submit(p, api_key=key)
    if on_submit:
        on_submit(pid)
    if no_wait:
        return {"prompt_id": pid, "outputs": [], "saved": []}

    entry = poll_history(pid, api_key=key)
    outs = extract_outputs(entry)
    saved = []
    for o in outs:
        if str(o.get("filename") or "").lower().endswith((".mp4", ".webm", ".png", ".jpg", ".webp")):
            path = download_output(o, download_dir)
            if path:
                saved.append(os.path.basename(path))
    return {"prompt_id": pid, "outputs": outs, "saved": saved}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ReActor 视频换脸")
    ap.add_argument("--face", required=True, help="参考脸图片（本地路径）")
    ap.add_argument("--video", required=True, help="源视频（本地路径）")
    ap.add_argument("--max-side", type=int, default=MAX_SIDE_DEFAULT, choices=MAX_SIDE_OPTIONS,
                    help="源视频最长边上限（等比缩放，保持源比例）")
    ap.add_argument("--interp", type=int, default=2, choices=[1, 2, 4], help="RIFE 插帧倍率")
    ap.add_argument("--compare", action="store_true", help="额外输出对比版")
    ap.add_argument("--out", default="outputs")
    a = ap.parse_args()
    print(json.dumps(run(a.face, a.video, max_side=a.max_side,
                         interp=a.interp, compare=a.compare,
                         download_dir=a.out), ensure_ascii=False, indent=1))