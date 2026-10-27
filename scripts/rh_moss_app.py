#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rh_moss_app.py —— MOSS 八步加速版 文生图（官方 ai-app 通道）

已发布 App: 2114331355738923010  ← moss八步加速版
对应工作流: 2114235743844761602

工作流结构（10 节点）
  [46] UNETLoader  moss_image_sft_int8_convrot
    └→ [55] LoRA  MOSS-Image-Base-8-steps-dmd @1.0（DMD 步数蒸馏，不是画风 LoRA）
         └→ [16] KSampler  steps×1.0 / cfg=1.0 / euler / simple
              ├ positive/negative → [4] CLIPTextEncode（cfg=1.0 下负向复用正向）
              └ latent → [6] EmptyFlux2LatentImage ← [23] ResolutionSelector
                   └→ [17] VAEDecode ← [22] VAELoader(flux2-vae)
                        └→ [18] SaveImage

暴露字段（2026-10-25 实测 /api/webapp/detail）
  23.aspect_ratio   分辨率比例（LIST，8 档，按 2 megapixels 反算尺寸）
  16.steps          步数（INT）
  4.text            提示词（STRING）

未暴露 → 无法在运行时改
  megapixels   锁定 2.0，即 9:16 时输出 1088×1920
  cfg / sampler / scheduler / denoise   锁定 1.0 / euler / simple / 1.0
  LoRA 强度     锁定 1.0
  seed          App 未暴露（工作流内为 randomize）

分辨率换算（megapixels=2.0, multiple=8）
  9:16  → 1088×1920     16:9  → 1920×1088
  3:4   → 1200×1600     4:3   → 1600×1200
  2:3   → 1152×1728     3:2   → 1728×1152
  1:1   → 1440×1440     21:9  → 2112×896

耗时实测（Standard / 9:16 / 2MP）
  4 步 → 约 12s      8 步 → 约 33~35s

对外函数
  run(...)             -> dict    一站式：提交 → 轮询 → 下载
  submit(...)          -> str     只提交，返回 taskId
  poll(task_id)        -> list    轮询到完成
  check()              -> str     打印暴露字段（排查用）
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

APP_ID = "2114331355738923010"      # moss八步加速版
WORKFLOW_ID = "2114235743844761602"  # 仅用于日志

# 字段 → nodeId 映射
NODE = {
    "ksampler":    "16",
    "pos":         "4",
    "resolution":  "23",
}

# App 实际暴露的字段（network 实测，兜底用；正常走 /api/webapp/detail 实时拉）
EXPOSED = {
    APP_ID: {
        ("23", "aspect_ratio"),
        ("16", "steps"),
        ("4", "text"),
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
DEFAULT_RATIO = "9:16 (Portrait Widescreen)"
DEFAULT_PROMPT = "棕色双麻花辫发型，两条麻花辫垂在胸前两侧，棕色头发，发尾微卷，齐刘海，中国年轻女性，东方亚洲面孔，黄皮肤，十八九岁少女，脸型小巧精致轮廓柔和流畅，五官秀气协调，眼睛大小自然，鼻型精致，嘴唇柔软，肌肤白皙细腻并保留真实皮肤质感，身材匀称，清纯温柔"

# 比例 → 实际输出尺寸（megapixels=2.0, multiple=8，仅用于日志/提示）
RATIO_SIZES = {
    "1:1 (Square)":                 (1440, 1440),
    "2:3 (Portrait Photo)":         (1152, 1728),
    "3:2 (Photo)":                  (1728, 1152),
    "3:4 (Portrait Standard)":      (1200, 1600),
    "4:3 (Standard)":               (1600, 1200),
    "9:16 (Portrait Widescreen)":   (1088, 1920),
    "16:9 (Widescreen)":            (1920, 1088),
    "21:9 (Ultrawide)":             (2112, 896),
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


def resolve_ratio(v):
    """比例短名 / 完整选项值 → 完整选项值。未命中原样返回（由后端校验）"""
    s = str(v or "").strip()
    if not s:
        return None
    return RATIO_ALIAS.get(s, RATIO_ALIAS.get(s.lower(), s))


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
def build_node_info(prompt_text=None, ratio=None, steps=None,
                    fields=None, webapp_id=APP_ID):
    """
    构造 nodeInfoList。只提交 App 真正暴露的字段——
    提交未暴露字段会被平台忽略或报错。

    ratio  可传完整选项值或短名（"9:16" / "portrait"），会自动展开。
    steps  4 = 约 12s（快，细节少）；8 = 约 33s（默认，细节足）。
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

    add(NODE["pos"], "text", prompt_text)
    add(NODE["resolution"], "aspect_ratio", resolve_ratio(ratio))
    add(NODE["ksampler"], "steps", steps)
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


def poll(task_id, max_wait=900, interval=5, verbose=True):
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
    name = os.path.basename(file_url.split("?")[0]) or f"out_{int(time.time())}.png"
    dest = os.path.join(str(dest_dir), name)
    req = urllib.request.Request(file_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


# ==================== 一站式入口 ====================
def run(prompt_text=None, ratio=DEFAULT_RATIO, steps=None,
        webapp_id=APP_ID, download_dir="outputs",
        on_submit=None, verbose=True, **_ignored):
    """
    MOSS 文生图：提示词 + 比例 + 步数。

    与 rh_zimage_app / rh_krea2_app 的签名差异：
      本 App 没有 LoRA 位、没有参考图、没有 denoise。
      宽高由 ratio 决定（megapixels 锁定 2.0），不接受 width/height。
      **_ignored 用于吃掉 webui 传来的 lob 参数，避免 TypeError。

    返回 dict：
      task_id / prompt_id / outputs / saved / webapp_id / ratio / steps
    """
    steps = int(steps) if steps else DEFAULT_STEPS
    ratio_full = resolve_ratio(ratio) or DEFAULT_RATIO

    ni = build_node_info(prompt_text=prompt_text, ratio=ratio_full,
                         steps=steps, webapp_id=webapp_id)
    if verbose:
        size = RATIO_SIZES.get(ratio_full)
        size_s = f"{size[0]}×{size[1]}" if size else "?"
        print(f"[App] {webapp_id} 提交 {len(ni)} 个字段: "
              f"{', '.join(n['nodeId'] + '.' + n['fieldName'] for n in ni)}")
        print(f"[参数] {ratio_full} → 约 {size_s} | steps={steps}")

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
        "steps": steps,
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
    lines.append("=== 比例 → 输出尺寸（megapixels 锁定 2.0）===")
    for k, (w, h) in RATIO_SIZES.items():
        lines.append(f"  {k:30} {w}×{h}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(check())
