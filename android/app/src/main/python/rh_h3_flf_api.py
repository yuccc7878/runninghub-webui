#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub 首帧驱动视频 —— App 版（ai-app/run 通道）
=====================================================
App: 2115566654334791681「minimaxh3锁首帧最长30s加速生成版」
工作流: 2115522426963775490「minimaxh3首尾帧」

通道差异（相对旧 Native /proxy 版）：
  旧：POST /proxy/<api-key>/upload/image → /prompt → /history/<prompt_id>
  新：POST /proxy/<api-key>/upload/image → /task/openapi/ai-app/run
      → /task/openapi/outputs（轮询 taskId）

App 暴露字段（/api/webapp/detail 实测 2026-10-30）：
    114.image         IMAGE   首帧图
    115.megapixels    FLOAT   分辨率，默认 0.4
    135.value         FLOAT   视频时长（秒）
    145.text          STRING  提示词

注意：App 未暴露 115.aspect_ratio，比例固定为工作流里的 9:16。
      传了也会被平台忽略，本脚本仍接受 aspect_ratio 参数只为兼容旧调用。

关键能力：
  1. 首帧 → 视频（单段）
  2. 时长支持 5–30 秒，秒 → 帧由工作流内 134 ComfyMathExpression 自动换算
     （17n+5 对齐：5s=124 / 10s=243 / 15s=362 / 20s=481 / 30s=736）
  3. 尾帧：本 App 未暴露 144，传 last_frame 会被忽略并告警

生成质量提示（实测经验）：
  0.4MP 在全身远景下人脸占比过小，容易崩脸。建议人脸重要时用 megapixels=1.0。
  时长超过 15 秒（362 帧）超出模型训练范围，后半段可能动作停滞或身份漂移。

自检：
  python3 rh_h3_flf_api.py
"""

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""                 # 由 rh_webui._inject_api_key 注入
BASE = "https://www.runninghub.ai"

APP_ID = "2115566654334791681"       # App 版（首帧驱动，最长 30s）
WORKFLOW_ID = "2115522426963775490"  # 仅用于日志

# 字段 → nodeId 映射（与 App 暴露字段一致）
NODE = {
    "first":      "114",   # LoadImage 首帧
    "resolution": "115",   # ResolutionSelector（仅 megapixels 暴露）
    "duration":   "135",   # PrimitiveFloat 时长（秒）
    "prompt":     "145",   # CR Text 提示词
}

# App 实际暴露的字段（network 实测，兜底用；正常走 /api/webapp/detail 实时拉）
EXPOSED = {
    APP_ID: {
        ("114", "image"),
        ("115", "megapixels"),
        ("135", "value"),
        ("145", "text"),
    },
}

# 轮询返回码
CODE_OK = 0
CODE_RUNNING = (804, 813)     # 运行中 / 排队中
CODE_FAILED = 805             # 任务失败
CODE_QUEUE_MAXED = 421        # 队列已满，可重试
CODE_NOT_FOUND = 807          # APIKEY_TASK_NOT_FOUND：任务号不存在/已过期

# 默认值（与 App 发布页一致）
DEFAULT_MEGAPIXELS = 0.4
DEFAULT_DURATION = 10
DEFAULT_RATIO = "9:16 (Portrait Widescreen)"   # App 未暴露，仅记录

# 比例 → 输出尺寸（megapixels=0.4, multiple=32，仅用于日志/提示）
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

# 比例短名 → 完整选项值
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


def resolve_ratio(v):
    """比例短名 / 完整选项值 → 完整选项值。未命中原样返回"""
    s = str(v or "").strip()
    if not s:
        return None
    return RATIO_ALIAS.get(s, RATIO_ALIAS.get(s.lower(), s))


def frames_of(seconds):
    """秒 → 帧数（24fps，17n+5 对齐）。与工作流内 134 表达式同算法"""
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        sec = DEFAULT_DURATION
    base = max(5, round(sec * 24))
    return base + ((5 - (base % 17)) % 17)


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


# ==================== 图片上传 ====================
def upload_image(file_path):
    """
    上传首帧图到 ComfyUI 存储，返回文件名。

    App 通道的 IMAGE 字段要求传「已在 ComfyUI 存储中的文件名」，
    不能直接传本地路径。走 /proxy/<key>/upload/image 上传。
    """
    url = f"{BASE}/proxy/{API_KEY}/upload/image"
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
    req.add_header("Authorization", API_KEY)

    d = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    name = d.get("name")
    if d.get("subfolder"):
        name = f"{d['subfolder']}/{name}"
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
def build_node_info(prompt_text=None, first_frame=None, ratio=None,
                    megapixels=None, duration=None,
                    fields=None, webapp_id=APP_ID):
    """
    构造 nodeInfoList。只提交 App 真正暴露的字段——
    提交未暴露字段会被平台忽略或报错。

    first_frame   已上传的 ComfyUI 文件名
    ratio         本 App 未暴露 aspect_ratio，传了也不提交，仅用于日志
    duration      秒数，工作流内 134 节点自动换算成帧
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

    add(NODE["first"],      "image",      first_frame)
    add(NODE["prompt"],     "text",       prompt_text)
    add(NODE["resolution"], "megapixels", megapixels)
    add(NODE["duration"],   "value",      duration)
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
    """轮询超时（排队/生成超过 max_wait），任务可能仍远端正跑，不能判死。"""


def poll(task_id, max_wait=2400, interval=5, verbose=True):
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
def run(
    prompt_text: str = None,
    first_frame_path: str = None,   # 本地首帧图路径（自动上传）
    last_frame_path: str = None,    # 保留参数；本 App 未暴露尾帧，会被忽略
    first_frame: str = None,        # 或直接传已上传文件名
    last_frame: str = None,         # 保留参数；本 App 未暴露尾帧，会被忽略
    aspect_ratio: str = DEFAULT_RATIO,   # App 未暴露，仅记录
    megapixels: float = DEFAULT_MEGAPIXELS,
    duration: float = DEFAULT_DURATION,
    steps: int = None,              # App 未暴露 steps（工作流内固定 8），仅记录
    noise_seed: int = None,         # App 未暴露，仅记录
    download_dir: str = "outputs",
    workflow_id: str = WORKFLOW_ID, # 仅记录
    lora: str = None,               # App 版内置 LoRA，忽略
    lora_strength: float = None,    # 忽略
    on_submit: callable = None,     # 提交成功回调 on_submit(taskId)
    verbose: bool = True,
    **_ignored,
) -> dict:
    """
    首帧 → 视频（App 通道）。返回 dict：
      prompt_id / task_id / outputs / videos / saved / first_frame
      ratio / megapixels / duration / length / app_id

    与旧 Native 版的签名保持兼容（first_frame_path / last_frame_path /
    aspect_ratio / steps / noise_seed / lora 都还在），rh_webui 无需改动。
    """
    mp = float(megapixels) if megapixels else DEFAULT_MEGAPIXELS
    dur = float(duration) if duration else DEFAULT_DURATION
    ratio_full = resolve_ratio(aspect_ratio) or DEFAULT_RATIO
    frames = frames_of(dur)

    # 本地路径 → 上传换文件名
    if first_frame_path and not first_frame:
        first_frame = upload_image(first_frame_path)
        if verbose:
            print(f"[首帧] {Path(first_frame_path).name} → {first_frame}")
    if last_frame_path or last_frame:
        if verbose:
            print("[尾帧] 本 App 未暴露 144 节点，尾帧参数被忽略")

    ni = build_node_info(prompt_text=prompt_text, first_frame=first_frame,
                         ratio=ratio_full, megapixels=mp, duration=dur)
    if verbose:
        size = RATIO_SIZES.get(ratio_full)
        size_s = f"{size[0]}×{size[1]}" if size else "?"
        print(f"[App] {APP_ID} 提交 {len(ni)} 个字段: "
              f"{', '.join(n['nodeId'] + '.' + n['fieldName'] for n in ni)}")
        print(f"[参数] {ratio_full} → 约 {size_s} | {dur}s ≈ {frames}帧 | mp={mp}")
        if not first_frame:
            print("[警告] 未传首帧图，任务可能失败")

    tid = submit(ni)
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
        "prompt_id": tid,          # 兼容旧接口字段名（此处是 taskId）
        "task_id": tid,
        "outputs": files,
        "videos": saved,
        "saved": saved,
        "first_frame": first_frame,
        "last_frame": None,
        "ratio": ratio_full,
        "megapixels": mp,
        "duration": dur,
        "length": frames,
        "app_id": APP_ID,
    }


# ==================== App 通道兼容接口 ====================
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
    lines = [f"=== App {APP_ID} → 工作流 {data.get('workflowId') or WORKFLOW_ID} ===",
             f"名称: {data.get('name')} | 暴露字段: {len(ins)}"]
    for n in ins:
        v = str(n.get("fieldValue"))
        if len(v) > 60:
            v = v[:60] + "..."
        lines.append(f"  {n.get('nodeId')}.{n.get('fieldName')} = {v}")
    lines.append("")
    lines.append("=== 时长 → 帧数（24fps，17n+5 对齐）===")
    for s in (5, 10, 15, 20, 25, 30):
        lines.append(f"  {s:2d}s → {frames_of(s)} 帧")
    lines.append("")
    lines.append("=== 比例 → 输出尺寸（megapixels=0.4, multiple=32）===")
    for k, (w, h) in RATIO_SIZES.items():
        lines.append(f"  {k:30} {w}×{h}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(check())