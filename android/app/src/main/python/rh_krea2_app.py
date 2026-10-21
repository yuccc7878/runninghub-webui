#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rh_krea2_app.py —— Krea2 双 App 调用（官方 ai-app 通道）
替代 rh_krea2_nsfw.py 的 /proxy 直调方式，改为调用已发布的 App：
  文生图 App  2113009215693500417  ← krea2快速省币
  图生图 App  2113025166677749762  ← krea2图生图极速省币

为什么改用 App 通道
  - /proxy 通道依赖自己拼 prompt，节点 ID 一变就要跟着改
  - App 通道用 nodeInfoList，只认识「暴露出来的字段」，节点内部结构由平台锁定
  - 图生图的参考图上传走 openapi 空间（api/xxx.png），平台自己解析

两个 App 的暴露字段（2026-10-22 实测 /api/webapp/detail）
  文生图 10 项
    41.text          提示词
    40.value         高度
    39.value         宽度
    36.steps         步数
    28.lora_name / 28.strength_model   画风 LoRA
    29.lora_name / 29.strength_model   写实 LoRA
    30.lora_name / 30.strength_model   身材 LoRA
  图生图 12 项（比文生图多 42.image 与 36.denoise）
    41.text  42.image  39.value  40.value  36.steps  36.denoise
    28.lora_name / 28.strength_model   画风 LoRA
    29.lora_name / 29.strength_model   写实 LoRA（2026-10-22 补勾）
    30.lora_name / 30.strength_model   身材 LoRA

  历史：首发版图生图 App 漏勾了 [29] lora_name（只有 strength_model），
  已重新发布补齐，现在两个 App 的 LoRA 三位都能在运行时换文件名。

LoRA 三位的接线（两个 App 一致）
    [27] UNETLoader → [28] LoRA 画风 → [29] LoRA 写实 → [30] LoRA 身材
        → [33] ModelSamplingAuraFlow → [36] KSampler.model

两条链路差异
    文生图： [39]Int(宽) [40]Int(高) → [35] EmptyLatentImage → [36].latent
    图生图： [42]LoadImage → [43]ImageScale → [44]VAEEncode → [36].latent

对外函数
  upload_image(path)   -> 文件名             上传参考图到 openapi 空间
  run_t2i(...)         -> dict               文生图
  run_i2i(...)         -> dict               图生图
  run(...)             -> dict               有 ref 走 i2i，无 ref 走 t2i
  poll(task_id)        -> list               轮询到完成，返回产物列表
  check()              -> str                打印两个 App 的暴露字段（排查用）
  list_loras()         -> str                打印可用 LoRA 别名
"""
import json
import os
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""            # 由 rh_webui._inject_api_key 注入
BASE = "https://www.runninghub.ai"

APP_T2I = "2113009215693500417"   # krea2快速省币
APP_I2I = "2113025166677749762"   # krea2图生图极速省币
WF_T2I = "2112996948134440962"    # 仅用于日志
WF_I2I = "2113012294128087041"

# 字段 → nodeId 映射（两个 App 的节点 ID 一致）
NODE = {
    "ksampler":   "36",
    "pos":        "41",       # CLIPTextEncode（标准节点，可暴露）
    "width":      "39",       # Int → EmptyLatentImage.width / ImageScale.width
    "height":     "40",       # Int → EmptyLatentImage.height / ImageScale.height
    "load_image": "42",       # 仅图生图
    "lora_style": "28",       # LoRA 画风（接 UNET）
    "lora_real":  "29",       # LoRA 写实（接 28）
    "lora_body":  "30",       # LoRA 身材（接 29）
}

# 各 App 实际暴露的字段（来自 /api/webapp/detail 的 inputNodes）
EXPOSED = {
    APP_T2I: {
        ("41", "text"),
        ("39", "value"), ("40", "value"),
        ("36", "steps"),
        ("28", "lora_name"), ("28", "strength_model"),
        ("29", "lora_name"), ("29", "strength_model"),
        ("30", "lora_name"), ("30", "strength_model"),
    },
    APP_I2I: {
        ("41", "text"), ("42", "image"),
        ("39", "value"), ("40", "value"),
        ("36", "steps"), ("36", "denoise"),
        ("28", "lora_name"), ("28", "strength_model"),
        ("29", "lora_name"), ("29", "strength_model"),
        ("30", "lora_name"), ("30", "strength_model"),
    },
}

# 轮询返回码
CODE_OK = 0
CODE_RUNNING = (804, 813)     # 运行中 / 排队中
CODE_FAILED = 805             # 任务失败
CODE_QUEUE_MAXED = 421        # 队列已满，可重试
CODE_NOT_FOUND = 807          # APIKEY_TASK_NOT_FOUND：任务号不存在/已过期

# 默认值（与 App 发布页一致，2026-10-22 实测）
DEFAULT_STEPS = 8
DEFAULT_DENOISE = 0.65        # 全图重绘档
DEFAULT_W, DEFAULT_H = 1080, 1920
# LoRA 别名表 —— 值是真实文件名，后端只认真实文件名
# 后端候选列表 7 万多项，报错里能看到长度；发布页 fieldData 返回 [[]] 只是前端下拉框为空
LORA_ALIAS = {
    # ---- 画风位（28）----
    "charm":      "krea2-Cc风情万种-风格.safetensors",
    "style":      "krea2-Cc风情万种-风格.safetensors",
    # ---- 写实位（29）----
    "realism":    "realism_engine_krea2_v3.1_new.safetensors",
    "real":       "realism_engine_krea2_v3.1_new.safetensors",
    # ---- 身材位（30）----
    "body":       "krea2-Cc-天魔-身材.safetensors",
    "tianmo":     "krea2-Cc-天魔-身材.safetensors",
}

# 各 App 发布时的实际默认强度 —— 必须按 App 分开！
# 两个 App 是独立发布的，同一个槽位的默认值并不一样（2026-10-22 用 apiCallDemo 实测）：
#   T2I: 28=1.0  29=0.5  30=0.85
#   I2I: 28=0.8  29=0.6  30=1.0
# 混用会导致「留空沿用默认」时强度悄悄偏移，出图效果与 App 页面预览不一致。
LORA_DEFAULT_STRENGTH = {
    APP_T2I: {"28": 1.0, "29": 0.5, "30": 0.85},
    APP_I2I: {"28": 0.8, "29": 0.6, "30": 1.0},
}
# 未知 App / 查不到时的兜底强度（不要再假设两 App 一致）
FALLBACK_LORA_STRENGTH = 0.8


def resolve_lora(name, strength=None, webapp_id=None, node_id=None):
    """
    把 LoRA 短名或文件名解析成 (真实文件名, 强度)。
    短名未命中表时按原样当文件名返回（由后端校验是否存在）。

    strength 为 None 时取「该 App、该槽位」的发布默认值，而不是全局写死一个数。
    返回 (None, None) 表示「不提交」，沿用 App 发布时的默认值。
    """
    if not name:
        return None, None
    fn = LORA_ALIAS.get(name, name)
    if strength is not None:
        return fn, float(strength)
    # 未显式给强度 → 取该 App 该槽位的发布默认
    per_app = LORA_DEFAULT_STRENGTH.get(webapp_id) or {}
    default_st = per_app.get(str(node_id), FALLBACK_LORA_STRENGTH)
    return fn, float(default_st)



def is_exposed(webapp_id, nid, fname):
    """该字段是否在该 App 的暴露列表里。优先拉实时列表，失败用静态表兜底。"""
    live = live_exposed(webapp_id)
    fields = live if live else EXPOSED.get(webapp_id, set())
    return (str(nid), fname) in fields


_live_cache = {}


def live_exposed(webapp_id):
    """从 /api/webapp/detail 实时拉暴露字段，带进程内缓存。失败返回 None。"""
    if webapp_id in _live_cache:
        return _live_cache[webapp_id]
    data = webapp_detail(webapp_id)
    ins = data.get("inputNodes") or []
    if not ins:
        return None
    s = {(str(n.get("nodeId")), n.get("fieldName")) for n in ins}
    _live_cache[webapp_id] = s
    return s


class _Detail(object):
    pass


detail = _Detail()
detail.T2I = APP_T2I
detail.I2I = APP_I2I
detail.WF_T2I = WF_T2I
detail.WF_I2I = WF_I2I
detail.NODE = NODE


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
def webapp_detail(webapp_id=APP_T2I):
    """拉 App 详情（暴露字段 + 默认值）。失败返回 {}"""
    d = _post("/api/webapp/detail", {"webappId": webapp_id}, timeout=20)
    if d.get("code") != 0:
        return {}
    return d.get("data") or {}


def exposed_fields(webapp_id):
    """
    返回该 App 实际暴露的 (nodeId, fieldName) 集合。
    优先从 /api/webapp/detail 实时拉，失败则用静态表兜底。
    """
    live = live_exposed(webapp_id)
    if live:
        return live
    return set(EXPOSED.get(webapp_id, set()))


# ==================== 上传参考图 ====================
def upload_image(file_path):
    """
    上传图片到 openapi 空间，返回 fileName（形如 api/xxx.png）。
    这个值可直接写进 42 LoadImage.image。
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"参考图不存在: {file_path}")
    data = p.read_bytes()
    ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    boundary = uuid.uuid4().hex

    parts = []
    for name, val in (("apiKey", API_KEY), ("fileType", "image")):
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{p.name}"\r\n'
        f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{BASE}/task/openapi/upload", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    d = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
    if d.get("code") != 0:
        raise RuntimeError(f"上传失败: {d}")
    return (d.get("data") or {}).get("fileName")


# ==================== 构造 nodeInfoList ====================
def build_node_info(webapp_id, prompt_text=None, width=None, height=None,
                    steps=None, denoise=None, image=None, fields=None,
                    lora_style=None, style_strength=None,
                    lora_real=None, real_strength=None,
                    lora_body=None, body_strength=None):
    """
    构造 nodeInfoList。fields 不传会自动拉取该 App 的暴露字段。
    只提交 App 真正暴露的字段——提交未暴露字段会被平台忽略或报错。

    LoRA 参数（三位，两个 App 都支持）：
      lora_style / style_strength  → 节点 28（画风，接 UNET）
      lora_real  / real_strength   → 节点 29（写实，接 28）★ 图生图 App 只有强度
      lora_body  / body_strength    → 节点 30（身材，接 29）

    传 None 表示「保持发布时的默认值」。
    空串一律视为「不提交」——后端对 lora_name 空串会报 433 value_not_in_list。
    """
    if fields is None:
        fields = exposed_fields(webapp_id)

    out = []

    def add(nid, fname, val):
        if val is None or val == "":
            return
        if (str(nid), fname) in fields:
            out.append({"nodeId": str(nid), "fieldName": fname,
                        "fieldValue": str(val)})

    add(NODE["pos"], "text", prompt_text)
    add(NODE["width"], "value", width)
    add(NODE["height"], "value", height)
    add(NODE["ksampler"], "steps", steps)
    add(NODE["ksampler"], "denoise", denoise)
    add(NODE["load_image"], "image", image)

    # LoRA 三位：短名先过别名表，名字与强度成对提交
    for slot, name, st in (("lora_style", lora_style, style_strength),
                           ("lora_real", lora_real, real_strength),
                           ("lora_body", lora_body, body_strength)):
        nid = NODE[slot]
        # 未显式给强度时，按「该 App 该槽位」的发布默认值补齐（两个 App 默认值不同）
        fn, sv = resolve_lora(name, st, webapp_id=webapp_id, node_id=nid)
        # 只有名称被暴露时才提交名称；强度独立判断
        if fn is not None and (str(nid), "lora_name") in fields:
            add(nid, "lora_name", fn)
            add(nid, "strength_model", sv)
        elif sv is not None:
            # 名称未暴露但强度暴露（兼容未来 App 版本可能收回 lora_name）：只调强度
            add(nid, "strength_model", sv)
    return out


# ==================== 提交 / 轮询 / 下载 ====================
def submit(webapp_id, node_info_list, retry=5, retry_wait=20):
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


def poll(task_id, max_wait=1800, interval=10, verbose=True):
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
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        dest += ".png"
    urllib.request.urlretrieve(file_url, dest)
    return dest


# ==================== 执行体 ====================
def _do_run(webapp_id, mode, prompt_text, width, height, steps, denoise,
            ref_image_path, download_dir, on_submit, verbose,
            lora_style=None, style_strength=None,
            lora_real=None, real_strength=None,
            lora_body=None, body_strength=None):
    """公共执行体：组装 → 提交 → 轮询 → 下载"""
    image = None
    if ref_image_path:
        image = upload_image(ref_image_path)
        if verbose:
            print(f"[上传参考图] {image}")

    fields = exposed_fields(webapp_id)
    ni = build_node_info(webapp_id, prompt_text=prompt_text, width=width,
                         height=height, steps=steps, denoise=denoise,
                         image=image, fields=fields,
                         lora_style=lora_style, style_strength=style_strength,
                         lora_real=lora_real, real_strength=real_strength,
                         lora_body=lora_body, body_strength=body_strength)
    if verbose:
        print(f"[App] {webapp_id} ({mode}) 提交 {len(ni)} 个字段: "
              f"{', '.join(n['nodeId'] + '.' + n['fieldName'] for n in ni)}")
        # 三位 LoRA 里没暴露名称的，明确提示
        for slot, nm in (("lora_style", lora_style), ("lora_real", lora_real),
                         ("lora_body", lora_body)):
            nid = NODE[slot]
            if nm and (str(nid), "lora_name") not in fields:
                print(f"[警告] 该 App 未暴露 {nid}.lora_name，"
                      f"{slot} 只能调强度，换名请求已忽略")

    tid = submit(webapp_id, ni)
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
        "mode": mode,
        "image": image,
    }


# ==================== 对外入口 ====================
def run_t2i(prompt_text, width=DEFAULT_W, height=DEFAULT_H,
            steps=DEFAULT_STEPS, download_dir="outputs",
            on_submit=None, verbose=True,
            lora_style=None, style_strength=None,
            lora_real=None, real_strength=None,
            lora_body=None, body_strength=None):
    """文生图：提示词 + 尺寸 + 3 组 LoRA（三位都能换名）"""
    return _do_run(APP_T2I, "文生图", prompt_text, width, height, steps, None,
                   None, download_dir, on_submit, verbose,
                   lora_style=lora_style, style_strength=style_strength,
                   lora_real=lora_real, real_strength=real_strength,
                   lora_body=lora_body, body_strength=body_strength)


def run_i2i(prompt_text, ref_image_path, width=DEFAULT_W, height=DEFAULT_H,
            steps=DEFAULT_STEPS, denoise=DEFAULT_DENOISE,
            download_dir="outputs", on_submit=None, verbose=True,
            lora_style=None, style_strength=None,
            lora_real=None, real_strength=None,
            lora_body=None, body_strength=None):
    """
    图生图：参考图 + 提示词 + 重绘强度 + 3 组 LoRA

    lora_style / style_strength  → 28 画风位（可换名）
    lora_real  / real_strength   → 29 写实位（可换名）
    lora_body  / body_strength   → 30 身材位（可换名）

    参考图只影响 latent 起点（走 42→43→44→36.latent），不进入 CLIP。
    所以提示词要写「要变成什么」，不是「原图是什么」——两者一致时画面几乎不变。
    """
    if not ref_image_path:
        raise ValueError("图生图需要 ref_image_path")
    return _do_run(APP_I2I, "图生图", prompt_text, width, height, steps, denoise,
                   ref_image_path, download_dir, on_submit, verbose,
                   lora_style=lora_style, style_strength=style_strength,
                   lora_real=lora_real, real_strength=real_strength,
                   lora_body=lora_body, body_strength=body_strength)


def run(prompt_text, ref_image_path=None, width=DEFAULT_W, height=DEFAULT_H,
        steps=None, denoise=None, download_dir="outputs",
        on_submit=None, verbose=True,
        lora_style=None, style_strength=None,
        lora_real=None, real_strength=None,
        lora_body=None, body_strength=None, **_ignored):
    """
    统一入口（兼容 rh_webui 旧调用签名）：
      有 ref_image_path → 图生图（支持 LoRA 运行时替换）
      无 ref_image_path → 文生图
    """
    if ref_image_path:
        return run_i2i(
            prompt_text, ref_image_path, width=width, height=height,
            steps=steps if steps is not None else DEFAULT_STEPS,
            denoise=denoise if denoise is not None else DEFAULT_DENOISE,
            download_dir=download_dir, on_submit=on_submit, verbose=verbose,
            lora_style=lora_style, style_strength=style_strength,
            lora_real=lora_real, real_strength=real_strength,
            lora_body=lora_body, body_strength=body_strength,
        )
    return run_t2i(
        prompt_text, width=width, height=height,
        steps=steps if steps is not None else DEFAULT_STEPS,
        download_dir=download_dir, on_submit=on_submit, verbose=verbose,
        lora_style=lora_style, style_strength=style_strength,
        lora_real=lora_real, real_strength=real_strength,
        lora_body=lora_body, body_strength=body_strength,
    )


# ==================== 自检 ====================
IS_APP_CHANNEL = True      # 通知 rh_webui：pid 是 App taskId，不是 /proxy 的 prompt_id
def extract_outputs(entry):
    """
    rh_webui._download_from_entry 的兼容接口。
    App 通道下 entry 就是 poll() 返回的产物列表 [{fileUrl, fileType, ...}]。
    """
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
    """
    查 App 通道任务状态（替代 /proxy/<key>/history/<pid>）。
    返回 {'status': success/error/running/queued/api_error, 'detail': ...}
    """
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
        # 任务号在平台上不存在（过期/被清理/写错），这不是网络问题：
        # 交给上层判定为「远端无此任务」，从而让用户决定是否重提
        return {"status": "not_found", "detail": d.get("msg") or "远端无此任务"}
    if code == 301:
        # 两种 301：apiKey 为空/无效（鉴权问题，可重试）
        #            taskId must be positive（参数坏，重试也没用）
        # 后者说明任务号本身是脏数据 → 判 not_found，让用户决定是否重提
        msg = (d.get("msg") or "").lower()
        if "taskid" in msg:
            return {"status": "not_found", "detail": d.get("msg") or "任务号无效"}
        return {"status": "api_error", "detail": "API Key 无效或未配置：301 " + (d.get("msg") or "")}
    if code == -1:
        return {"status": "api_error", "detail": d.get("msg") or "网络异常"}
    return {"status": "api_error", "detail": f"未知返回码 {code}: {d.get('msg') or ''}"}

def list_loras():
    """打印可用 LoRA 别名（运行时可直接把短名传给 lora_* 参数）"""
    lines = ["=== LoRA 别名（传短名即可，会自动展开成真实文件名）==="]
    for k, fn in sorted(LORA_ALIAS.items()):
        lines.append(f"  {k:10} {fn}")
    lines.append("")
    lines.append("=== 留空强度时沿用的发布默认值（两个 App 不同，不要混用）===")
    for label, wid in (("文生图", APP_T2I), ("图生图", APP_I2I)):
        d = LORA_DEFAULT_STRENGTH.get(wid) or {}
        lines.append(f"  {label}: 28画风={d.get('28')}  "
                     f"29写实={d.get('29')}  30身材={d.get('30')}")
    lines.append("")
    lines.append("也可以直接传任意真实文件名，只要它在平台 loras 目录里。")
    return "\n".join(lines)


def api_call_demo(webapp_id):
    """
    GET /api/webapp/apiCallDemo —— 平台给出的「可直接提交」字段列表 + 当前默认值。
    比 /api/webapp/detail 更贴近实际能提交的格式，用来核对本模块写死的默认值。
    """
    d = _get(f"/api/webapp/apiCallDemo?apiKey={API_KEY}&webappId={webapp_id}", timeout=30)
    if d.get("code") != 0:
        return {}
    return d.get("data") or {}


def sync_defaults(verbose=True):
    """
    用平台 apiCallDemo 的权威默认值，核对本模块的 LORA_DEFAULT_STRENGTH。
    返回 {"ok": bool, "diff": {app: {node: (平台值, 本地值)}}}
    只核对，不自动改代码——发现差异请手动更新 LORA_DEFAULT_STRENGTH。
    """
    diff = {}
    for label, wid in (("文生图", APP_T2I), ("图生图", APP_I2I)):
        demo = api_call_demo(wid)
        ni = demo.get("nodeInfoList") or []
        plat = {str(n.get("nodeId")): n.get("fieldValue")
                for n in ni if n.get("fieldName") == "strength_model"}
        mine = LORA_DEFAULT_STRENGTH.get(wid) or {}
        for nid, local in mine.items():
            raw = plat.get(nid)
            if raw is None:
                continue
            try:
                pv = float(raw)
            except (TypeError, ValueError):
                continue
            if abs(pv - float(local)) > 1e-6:
                diff.setdefault(label, {})[nid] = (pv, float(local))
    ok = not diff
    if verbose:
        if ok:
            print("[sync_defaults] 两个 App 的 LoRA 默认强度与平台一致 ✅")
        else:
            print("[sync_defaults] 发现差异（平台值 vs 本模块 LORA_DEFAULT_STRENGTH）：")
            for label, items in diff.items():
                for nid, (pv, lv) in sorted(items.items()):
                    print(f"  {label} 节点{nid}: 平台={pv}  本地={lv}")
            print("→ 请更新 rh_krea2_app.LORA_DEFAULT_STRENGTH 与 rh_webui.REGISTRY 的 def_strength")
    return {"ok": ok, "diff": diff}


def check():
    """打印两个 App 的暴露字段与默认值，用于排查字段名变更"""
    lines = []
    for name, wid, wf in (("文生图", APP_T2I, WF_T2I), ("图生图", APP_I2I, WF_I2I)):
        data = webapp_detail(wid)
        ins = data.get("inputNodes") or []
        lines.append(f"=== {name} App {wid} → 工作流 {wf} ===")
        lines.append(f"名称: {data.get('name')} | 暴露字段: {len(ins)}")
        for n in ins:
            v = str(n.get("fieldValue"))
            if len(v) > 60:
                v = v[:60] + "..."
            lines.append(f"  {n.get('nodeId')}.{n.get('fieldName')} = {v}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    print(check())
    print()
    print(list_loras())
    print()
    sync_defaults()
