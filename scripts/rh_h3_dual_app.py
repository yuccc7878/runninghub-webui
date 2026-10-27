#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rh_h3_dual_app.py —— MiniMax H3 双参考图视频（官方 ai-app 通道）
=================================================================

已发布 App: 2114838999247724546  ← MiniMax H3 双参考图视频
对应工作流: 2114436343560867841

与旧模块的区别
  rh_workflow_api.py  走 /proxy/<key>/prompt 原生通道，工作流 2108502955011149825
                      （MiniMaxH3Unified 全能版，5 图 + 视频 + 音频参考）
  本模块               走 /task/openapi/ai-app/run App 通道，工作流 2114436343560867841
                      （MiniMaxH3ReferenceToVideo 双参考图版，2 图，带音频输出）

为什么改走 App 通道
  App 通道的字段由 /api/webapp/validate 白名单控制，只要发布页配好就能稳定提交；
  而双图工作流里的动态孔 ref_images.* 在 getJsonApiFormat 序列化时会被丢弃，
  走 /proxy 反而更麻烦。

工作流结构（23 节点 / V5）
  [127] UNETLoader  minimax_h3
   ├→ [189] LoRA 加速 @1.0     ← 8 步蒸馏
   └→ [161] LoRA 风格 @0.9
        └→ [126] BasicGuider ← [184] MiniMaxH3ReferenceToVideo
                                  ├ clip       ← [128] CLIPLoader(qwen3vl_32b)
                                  ├ vae        ← [119] VAELoader(video_vae)
                                  ├ audio_vae  ← [120] VAELoader(audio_vae)
                                  ├ ref_image_0 ← [183] LoadImage  女仆/双麻花辫
                                  ├ ref_image_1 ← [190] LoadImage  兔女郎/大波浪
                                  ├ prompt     ← [144] CR Text
                                  ├ width      ← [115] ResolutionSelector
                                  ├ height     ← [115] ResolutionSelector
                                  ├ length     ← [192] PrimitiveInt  （帧数，非秒数）
                                  └ ref_image_size
             └→ [125] SamplerCustomAdvanced ← [129] RandomNoise / [181] BasicScheduler
                  └→ [177] MiniMaxH3AVDecodeT8
                       └→ [178] VHS_VideoCombine（视频 + 音频合流）

暴露字段（2026-10-27 实测 /api/webapp/detail）
  183.image          IMAGE    参考图 1 → <Picture 1>
  190.image          IMAGE    参考图 2 → <Picture 2>
  144.text           STRING   视频提示词
  181.steps          INT      采样步数，默认 8
  115.aspect_ratio   LIST     画面比例
  115.megapixels     FLOAT    分辨率，默认 0.4
  192.value          INT      帧数，默认 243

帧数换算（24fps，17n+5 对齐）
  5s  → 124
  10s → 243
  15s → 362
  ★ App 端只能填帧数，工作流里没有秒→帧的转换节点
    （H3DurationPlanner / CustomCombo 等第三方节点会被 validate 过滤）

分辨率换算（multiple=32 对齐）
  9:16  @0.4MP → 480×832
  16:9  @0.4MP → 832×480

耗时实测（Standard）
  9:16 / 0.4MP / 243帧 / 8步 → 约 2.5 分钟

对外函数
  run(...)        -> dict   一站式：提交 → 轮询 → 下载
  submit(...)     -> str    只提交，返回 taskId
  poll(task_id)   -> list   轮询到完成
  check()         -> str    打印暴露字段（排查用）
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""            # 由 rh_webui._inject_api_key 注入
BASE = "https://www.runninghub.ai"

APP_ID = "2114838999247724546"       # MiniMax H3 双参考图视频
WORKFLOW_ID = "2114436343560867841"  # 仅用于日志

# 字段 → nodeId 映射
NODE = {
    "ref_a":      "183",   # LoadImage  → Picture 1
    "ref_b":      "190",   # LoadImage  → Picture 2
    "prompt":     "144",   # CR Text
    "steps":      "181",   # BasicScheduler
    "resolution": "115",   # ResolutionSelector（aspect_ratio + megapixels 同节点）
    "length":     "192",   # PrimitiveInt（帧数）
}

# App 实际暴露的字段（network 实测，兜底用；正常走 /api/webapp/detail 实时拉）
EXPOSED = {
    APP_ID: {
        ("183", "image"),
        ("190", "image"),
        ("144", "text"),
        ("181", "steps"),
        ("115", "aspect_ratio"),
        ("115", "megapixels"),
        ("192", "value"),
    },
}

# 轮询返回码
CODE_OK = 0
CODE_RUNNING = (804, 813)     # 运行中 / 排队中
CODE_FAILED = 805             # 任务失败
CODE_QUEUE_MAXED = 421        # 队列已满，可重试
CODE_NOT_FOUND = 807          # APIKEY_TASK_NOT_FOUND：任务号不存在/已过期

# 默认值（与 App 发布页一致）
DEFAULT_STEPS = 8
DEFAULT_MEGAPIXELS = 0.4
DEFAULT_LENGTH = 243          # 243 帧 = 10 秒
DEFAULT_RATIO = "9:16 (Portrait Widescreen)"

# 比例 → 实际输出尺寸（megapixels=0.4, multiple=32，仅用于日志/提示）
RATIO_SIZES = {
    "1:1 (Square)":                 (640, 640),
    "2:3 (Portrait Photo)":         (512, 768),
    "3:2 (Photo)":                  (768, 512),
    "3:4 (Portrait Standard)":      (544, 736),
    "4:3 (Standard)":               (736, 544),
    "9:16 (Portrait Widescreen)":   (480, 832),
    "16:9 (Widescreen)":            (832, 480),
    "21:9 (Ultrawide)":             (896, 384),
}

# 比例短名 → 完整选项值（前端下拉可以直接传短名）
RATIO_ALIAS = {
    "1:1":  "1:1 (Square)",
    "2:3":  "2:3 (Portrait Photo)",
    "3:2":  "3:2 (Photo)",
    "3:4":  "3:4 (Portrait Standard)",
    "4:3":  "4:3 (Standard)",
    "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "21:9": "21:9 (Ultrawide)",
    "portrait": "9:16 (Portrait Widescreen)",
    "landscape": "16:9 (Widescreen)",
    "square": "1:1 (Square)",
}

# 秒 → 帧数（24fps，17n+5 对齐）。App 端填帧数，这里只做前端友好换算
SECONDS_TO_FRAMES = {
    5: 124,
    10: 243,
    15: 362,
}


def resolve_ratio(v):
    """比例短名 / 完整选项值 → 完整选项值。未命中原样返回（由后端校验）"""
    s = str(v or "").strip()
    if not s:
        return None
    return RATIO_ALIAS.get(s, RATIO_ALIAS.get(s.lower(), s))


def resolve_length(v):
    """
    帧数或秒数 → 帧数。

    前端可以传 duration=10（秒），也可以传 length=243（帧）。
    传整数且 <= 15 时视为秒数，查表换算；否则原样当帧数。
    这样 webui 上既能写「时长 10 秒」，也能直接填帧数。
    """
    if v is None or v == "":
        return DEFAULT_LENGTH
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return DEFAULT_LENGTH
    # <= 15 视为秒数
    if 0 < n <= 15:
        return SECONDS_TO_FRAMES.get(n, n * 24 + 3)
    return n


# ==================== 基础请求 ====================
def _post(path, body, timeout=40):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_KEY}"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"code": e.code, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def _get(path, timeout=30):
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {API_KEY}", "accept": "application/json"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"code": e.code, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


# ==================== 参考图上传 ====================
def upload_image(file_path):
    """
    上传参考图到 ComfyUI 存储，返回文件名。

    App 通道的 IMAGE 字段要求传「已在 ComfyUI 存储中的文件名」，
    不能直接传本地路径。走 /proxy/<key>/upload/image 上传。
    """
    import uuid

    key = API_KEY
    url = f"{BASE}/proxy/{key}/upload/image"
    data = Path(file_path).read_bytes()
    ct = "image/png" if str(file_path).lower().endswith(".png") else "image/jpeg"
    boundary = uuid.uuid4().hex

    body = b""
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="image"; '
             f'filename="{Path(file_path).name}"\r\n').encode()
    body += f"Content-Type: {ct}\r\n\r\n".encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("accept", "application/json")
    req.add_header("content-type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", key)

    d = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    name = d.get("name") or d.get("subfolder") and f"{d['subfolder']}/{d['name']}"
    if not name:
        raise RuntimeError(f"上传失败，未返回文件名: {d}")
    return name


# ==================== App 详情 ====================
def webapp_detail(webapp_id=APP_ID):
    """拉 App 详情（暴露字段 + 默认值）。失败返回 {}"""
    d = _post("/api/webapp/detail", {"webappId": webapp_id}, timeout=20)
    if d.get("code") != 0:
        return {}
    return d.get("data") or {}


def exposed_fields(webapp_id=APP_ID):
    """
    返回该 App 实际暴露的 (nodeId, fieldName) 集合。
    优先从 /api/webapp/detail 实时拉，失败则用静态表兜底。
    """
    data = webapp_detail(webapp_id)
    ins = data.get("inputNodes") or []
    if ins:
        return {(str(n.get("nodeId")), n.get("fieldName")) for n in ins}
    return set(EXPOSED.get(webapp_id, set()))


# ==================== 构造 nodeInfoList ====================
def build_node_info(prompt_text=None, ref_a=None, ref_b=None, ratio=None,
                    megapixels=None, steps=None, length=None, duration=None,
                    fields=None, webapp_id=APP_ID):
    """
    构造 nodeInfoList。只提交 App 真正暴露的字段——
    提交未暴露字段会被平台忽略或报错。

    ref_a / ref_b  参考图文件名（先调 upload_image 上传）
    ratio          可传完整选项值或短名（"9:16" / "portrait"）
    duration       秒数（5/10/15），自动换算成帧数
    length         帧数（124/243/362），与 duration 二选一，length 优先
    """
    if fields is None:
        fields = exposed_fields(webapp_id)

    out = []

    def add(nid, fname, val):
        # 空串一律视为「不提交」——后端对空串会报 value_not_in_list
        if val is None or val == "":
            return
        if (str(nid), fname) in fields:
            out.append({"nodeId": str(nid), "fieldName": fname,
                        "fieldValue": str(val)})

    add(NODE["ref_a"],  "image",         ref_a)
    add(NODE["ref_b"],  "image",         ref_b)
    add(NODE["prompt"], "text",          prompt_text)
    add(NODE["steps"],  "steps",         steps)
    add(NODE["resolution"], "aspect_ratio", resolve_ratio(ratio))
    add(NODE["resolution"], "megapixels", megapixels)
    add(NODE["length"], "value",         resolve_length(length if length else duration))
    return out


# ==================== 提交 / 轮询 / 下载 ====================
def submit(node_info_list, webapp_id=APP_ID, retry=5, retry_wait=20):
    """提交任务，返回 taskId。队列满（421）自动重试。"""
    body = {"webappId": webapp_id, "apiKey": API_KEY,
            "nodeInfoList": node_info_list}
    last = None
    for i in range(retry):
        d = _post("/task/openapi/ai-app/run", body, timeout=40)
        code = d.get("code")
        if code == CODE_OK:
            tid = (d.get("data") or {}).get("taskId")
            if not tid:
                raise RuntimeError(f"未返回 taskId: {d}")
            return tid
        last = d
        if code == CODE_QUEUE_MAXED and i < retry - 1:
            print(f"[队列满] 第 {i + 1} 次重试，{retry_wait}s 后再试")
            time.sleep(retry_wait)
            continue
        break
    raise RuntimeError(f"提交失败: {json.dumps(last, ensure_ascii=False)[:400]}")


class PollTimeout(Exception):
    """轮询超时，任务可能仍在远端正跑"""


def poll(task_id, max_wait=1200, interval=5, verbose=True):
    """轮询到完成，返回产物列表 [{fileUrl, fileType, ...}]"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        d = _post("/task/openapi/outputs",
                  {"taskId": task_id, "apiKey": API_KEY}, timeout=30)
        code = d.get("code")
        if code == CODE_OK:
            return d.get("data") or []
        if code == CODE_FAILED:
            fr = (d.get("data") or {}).get("failedReason") or {}
            node = fr.get("node_name") or "?"
            etype = fr.get("exception_type") or "?"
            tb = fr.get("traceback") or ""
            if isinstance(tb, str):
                try:
                    tb = json.loads(tb)
                except Exception:
                    pass
            if isinstance(tb, list):
                tb = "\n".join(str(x) for x in tb)
            tail = tb.strip().splitlines()[-1] if tb.strip() else ""
            raise RuntimeError(f"任务失败 [{node}] {etype}: {tail[:300]}")
        if verbose and int(time.time() - t0) % 30 < interval:
            print(f"[轮询] {int(time.time() - t0)}s code={code}")
        time.sleep(interval)
    raise PollTimeout(f"轮询超时 {max_wait}s: {task_id}")


def download(file_url, dest_dir="outputs"):
    """下载产物到本地，返回路径"""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    name = os.path.basename(file_url.split("?")[0]) or f"out_{int(time.time())}.mp4"
    dest = os.path.join(str(dest_dir), name)
    req = urllib.request.Request(file_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


# ==================== 一站式入口 ====================
def run(prompt_text=None, ref_a=None, ref_b=None,
        ratio=DEFAULT_RATIO, megapixels=DEFAULT_MEGAPIXELS,
        steps=None, length=None, duration=None,
        webapp_id=APP_ID, download_dir="outputs",
        on_submit=None, verbose=True, **_ignored):
    """
    MiniMax H3 双参考图视频：两张参考图 + 提示词 → 带声音的视频。

    与 rh_moss_app 的差异：
      本 App 有两个 IMAGE 位（ref_a / ref_b），对应提示词里的
      <Picture 1> / <Picture 2>；输出是视频（含音频），不是图片。

    ref_a / ref_b  可以是本地路径（自动上传）或已上传的文件名。
    duration       秒数（5/10/15），或直接用 length 传帧数。
    **_ignored     吃掉 webui 传来的多余参数，避免 TypeError。

    返回 dict：
      task_id / prompt_id / outputs / saved / webapp_id
      ratio / megapixels / steps / length / ref_a / ref_b
    """
    steps = int(steps) if steps else DEFAULT_STEPS
    mp = float(megapixels) if megapixels else DEFAULT_MEGAPIXELS
    ratio_full = resolve_ratio(ratio) or DEFAULT_RATIO
    frames = resolve_length(length if length else duration)

    # 本地路径 → 上传换文件名
    def ensure_uploaded(v, tag):
        if not v:
            return None
        s = str(v)
        if os.path.isfile(s):
            name = upload_image(s)
            if verbose:
                print(f"[上传] {tag}: {os.path.basename(s)} → {name}")
            return name
        return s

    fa = ensure_uploaded(ref_a, "Picture 1")
    fb = ensure_uploaded(ref_b, "Picture 2")

    ni = build_node_info(prompt_text=prompt_text, ref_a=fa, ref_b=fb,
                         ratio=ratio_full, megapixels=mp, steps=steps,
                         length=frames, webapp_id=webapp_id)
    if verbose:
        size = RATIO_SIZES.get(ratio_full)
        size_s = f"{size[0]}×{size[1]}" if size else "?"
        print(f"[App] {webapp_id} 提交 {len(ni)} 个字段: "
              f"{', '.join(n['nodeId'] + '.' + n['fieldName'] for n in ni)}")
        print(f"[参数] {ratio_full} → 约 {size_s} | {frames}帧 ≈ {frames/24:.1f}s | steps={steps}")

    tid = submit(ni, webapp_id=webapp_id)
    if verbose:
        print(f"[提交成功] taskId = {tid}")
    if on_submit:
        on_submit(tid)

    files = poll(tid, verbose=verbose)
    saved = []
    for f_ in files:
        url = f_.get("fileUrl") or f_.get("url")
        if url:
            path = download(url, download_dir)
            saved.append(path)
            if verbose:
                print(f"  已下载: {path}")

    return {
        "task_id": tid,
        "prompt_id": tid,          # 兼容旧接口字段名
        "outputs": files,
        "saved": saved,
        "webapp_id": webapp_id,
        "ratio": ratio_full,
        "megapixels": mp,
        "steps": steps,
        "length": frames,
        "ref_a": fa,
        "ref_b": fb,
    }


# ==================== 自检 / 兼容接口 ====================
IS_APP_CHANNEL = True      # 通知 rh_webui：pid 是 App taskId，不是 /proxy 的 prompt_id


def extract_outputs(entry):
    """rh_webui._download_from_entry 的兼容接口"""
    if isinstance(entry, list):
        return [o for o in entry if isinstance(o, dict) and (o.get("fileUrl") or o.get("url"))]
    return []


def download_output(out, dest_dir="outputs"):
    """rh_webui._download_from_entry 的兼容接口：按产物条目下载"""
    url = out.get("fileUrl") or out.get("url")
    if not url:
        return None
    return download(url, dest_dir)


def check_app_task(task_id):
    """查 App 通道任务状态。返回 {status, detail}"""
    d = _post("/task/openapi/outputs",
              {"taskId": task_id, "apiKey": API_KEY}, timeout=30)
    code = d.get("code")
    if code == CODE_OK:
        return {"status": "success", "detail": d.get("data") or []}
    if code == CODE_FAILED:
        fr = (d.get("data") or {}).get("failedReason") or {}
        return {"status": "error", "detail": f"{fr.get('node_name') or '?'} "
                                             f"{fr.get('exception_type') or ''}".strip()}
    if code in CODE_RUNNING:
        return {"status": "running", "detail": {"code": code}}
    if code == CODE_QUEUE_MAXED:
        return {"status": "queued", "detail": {"code": code}}
    if code == CODE_NOT_FOUND:
        return {"status": "not_found", "detail": d.get("msg") or "远端无此任务"}
    if code == 301:
        msg = (d.get("msg") or "").lower()
        if "taskid" in msg:
            return {"status": "not_found", "detail": d.get("msg") or "任务号无效"}
        return {"status": "api_error", "detail": "API Key 无效或未配置：301 " + (d.get("msg") or "")}
    if code == -1:
        return {"status": "api_error", "detail": d.get("msg") or "网络异常"}
    return {"status": "api_error", "detail": f"未知返回码 {code}: {d.get('msg') or ''}"}


def check():
    """打印 App 的暴露字段与默认值，用于排查字段名变更"""
    data = webapp_detail(APP_ID)
    ins = data.get("inputNodes") or []
    lines = [f"=== App {APP_ID} → 工作流 {WORKFLOW_ID} ===",
             f"名称: {data.get('name')} | 暴露字段: {len(ins)}"]
    for n in ins:
        v = str(n.get("fieldValue"))
        if len(v) > 60:
            v = v[:60] + "..."
        lines.append(f"  {n.get('nodeId')}.{n.get('fieldName')} = {v}")
    lines.append("")
    lines.append("=== 比例 → 输出尺寸（megapixels=0.4, multiple=32）===")
    for k, (w, h) in RATIO_SIZES.items():
        lines.append(f"  {k:30} {w}×{h}")
    lines.append("")
    lines.append("=== 帧数换算（24fps，17n+5 对齐）===")
    for s, f in SECONDS_TO_FRAMES.items():
        lines.append(f"  {s:2d}s → {f} 帧")
    return "\n".join(lines)


if __name__ == "__main__":
    print(check())
