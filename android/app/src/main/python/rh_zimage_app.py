# -*- coding: utf-8 -*-
"""
rh_zimage_app.py —— zimage 双 App 调用（官方 ai-app 通道）

替代 rh_zimage_api / rh_zimage_v2 的 /proxy 直调方式，改为调用已发布的 App：
  文生图 App  2112630365772128258 → 工作流 2112320896592891905
  图生图 App  2112645746440085506 → 工作流 2112633650515902466

为什么改用 App 通道：
  - /proxy 通道依赖自己拼 prompt，节点 ID 一变就要跟着改
  - App 通道用 nodeInfoList，只认识「暴露出来的字段」，节点内部结构由平台锁定
  - 图生图的参考图上传走 openapi 空间（api/xxx.png），平台自己解析，不用关心 input 目录

⚠️ 图生图 App 的演进（2026-10-21）
   首发版绑定的是 21 节点 inpaint 版工作流，运行时崩溃：
     ComfyUI-Apt_Preset/NodeBasic/mask_human.py:346 segment_clothes
     RuntimeError: Clothes segmentation error
   已改为 16 节点全图重绘版（denoise=0.65，latent 接 VAEEncode）并重新发布。
   重新发布不产生新 webappId，只更新版本内容（updateTime 变化，publishTime 不变）。
   实测通过：taskid 2112651549933629441，1080×1920，约 40 秒。

暴露参数（实测 /api/webapp/detail）
  文生图 4 项：4.steps  5.text  16.value  17.value
  图生图 10 项：4.steps  4.denoise  5.text  16.value  17.value  30.image
                20.lora_name  20.strength_model  14.lora_name  14.strength_model

LoRA 运行时替换（2026-10-21 实测验证）
    图生图工作流的两个 LoraLoaderModelOnly 节点已串接进 model 链：
        UNETLoader(1) → LoRA(14 瑶光 @0.3) → LoRA(20 如梦似幻 @0.25) → KSampler(4).model
    发布时暴露 lora_name + strength_model 两列（共 4 个字段），
    运行时传 nodeInfoList 即可换成任意别的 LoRA，不必改工作流：
        {"nodeId":"14","fieldName":"lora_name","fieldValue":"其他LoRA.safetensors"}

    两个坑（实测踩过，2026-10-21）
      1. lora_name 必须传**真实文件名**，且必须在平台 loras 目录的候选列表里。
         空串 ""、"None"、或不存在的文件名都会被后端拒绝：
             value_not_in_list: lora_name: '' not in (list of length 75293)
         报错里那个 75293 说明后端候选列表是完整的——只是发布页 fieldData
         返回 [[]] 让前端下拉框显示为空，API 提交不受影响。
         实测通过的三个真实文件名：
             zimage-turbo_Cc-风情万种-人像.safetensors
             无滤真实_V2.safetensors
             ZIT-flow-dpo-lora.safetensors
         要「不加载某个 LoRA」不能靠传空值，只能：
           a) 把强度设成 0（LoRA 加载但权重为 0，效果等价于不生效）
           b) 换一个中性 LoRA
           c) 改工作流把该节点 bypass 后重新发布（需要新的 App 版本）
      2. strength_model 是 FLOAT，传数字或字符串数字都行，后端会转。
      3. 提交未暴露的字段会被平台忽略，不会报错——所以传错 nodeId 时
         表现为「参数没生效」而不是「报错」，排查时先看 App 的 inputNodes。

对外函数
  upload_image(path) -> 文件名         上传参考图到 openapi 空间
  run_t2i(...)      -> dict            文生图
  run_i2i(...)      -> dict            图生图
  run(...)          -> dict            有 ref 走 i2i，无 ref 走 t2i
  poll(task_id)     -> list            轮询到完成，返回产物列表
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

APP_T2I = "2112630365772128258"   # zimage文生图
APP_I2I = "2112645746440085506"   # zimage图生图

WF_T2I = "2112320896592891905"    # 仅用于日志
WF_I2I = "2112633650515902466"

# 字段 → nodeId 映射（两个 App 的节点 ID 恰好一致）
NODE = {
    "ksampler":   "4",
    "pos":        "5",
    "width":      "16",
    "height":     "17",
    "load_image": "30",
    "lora_a":     "14",      # Kook_Zimage_瑶光      → 直接接 UNET
    "lora_b":     "20",      # Kook_Zimage_如梦似幻  → 接 lora_a
}

# 各 App 实际暴露的字段（来自 /api/webapp/detail 的 inputNodes）
EXPOSED = {
    APP_T2I: {("4", "steps"), ("5", "text"), ("16", "value"), ("17", "value")},
    APP_I2I: {("4", "steps"), ("4", "denoise"), ("5", "text"),
              ("16", "value"), ("17", "value"), ("30", "image"),
              ("14", "lora_name"), ("14", "strength_model"),
              ("20", "lora_name"), ("20", "strength_model")},
}

# 轮询返回码
CODE_OK = 0
CODE_RUNNING = (804, 813)     # 运行中 / 排队中
CODE_FAILED = 805             # 任务失败
CODE_QUEUE_MAXED = 421        # 队列已满，可重试

# 默认值（与 App 发布页一致，2026-10-21 实测）
DEFAULT_STEPS = 6
DEFAULT_DENOISE = 0.65        # 全图重绘档，App 发布值已同步为 0.65
DEFAULT_W, DEFAULT_H = 1080, 1920

# LoRA 别名表（沿用 rh_zimage_v2.KNOWN_LORAS 的真实文件名）
# lora_name 传真实文件名时后端才会接受，短名要先过这张表展开。
LORA_ALIAS = {
    "charm":    ("zimage-turbo_Cc-风情万种-人像.safetensors", 1.0),
    "nofilter": ("无滤真实_V2.safetensors", 0.7),
    "flowdpo":  ("ZIT-flow-dpo-lora.safetensors", 0.8),
    "model":    ("zimage_全能模特.safetensors", 0.45),
    "peach":    ("🍑ins-V2-Zimage.safetensors", 0.9),
    "zanime":   ("Z-Anime04.safetensors", 1.0),
}


def resolve_lora(name, strength=None):
    """
    把 LoRA 短名或文件名解析成 (真实文件名, 强度)。
    短名未命中表时按原样当文件名返回（由后端校验是否存在）。
    """
    if not name:
        return None, None
    info = LORA_ALIAS.get(name)
    if info:
        fn, default_st = info
        return fn, float(strength if strength is not None else default_st)
    return name, (float(strength) if strength is not None else 0.9)


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
    data = webapp_detail(webapp_id)
    ins = data.get("inputNodes") or []
    if ins:
        return {(str(n.get("nodeId")), n.get("fieldName")) for n in ins}
    return set(EXPOSED.get(webapp_id, set()))


# ==================== 上传参考图 ====================
def upload_image(file_path):
    """
    上传图片到 openapi 空间，返回 fileName（形如 api/xxx.png）。
    这个值可直接写进 30 LoadImage.image。
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
                    lora_name=None, lora_strength=None,
                    lora_name_b=None, lora_strength_b=None):
    """
    构造 nodeInfoList。fields 不传会自动拉取该 App 的暴露字段。
    只提交 App 真正暴露的字段——提交未暴露字段会被平台忽略或报错。

    LoRA 参数（仅图生图 App 支持）：
      lora_name / lora_strength        → 节点 14（瑶光位，接 UNET）
      lora_name_b / lora_strength_b    → 节点 20（如梦似幻位，接 14）
    传 None 表示「保持发布时的默认值」，传 "" 表示「该节点不加载 LoRA」。
    """
    if fields is None:
        fields = exposed_fields(webapp_id)

    out = []

    def add(nid, fname, val):
        # 空串一律视为「不提交」——后端对 lora_name 空串会报 433 value_not_in_list
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

    # LoRA：短名先过别名表，名字与强度成对提交
    fa, sa = resolve_lora(lora_name, lora_strength)
    fb, sb = resolve_lora(lora_name_b, lora_strength_b)
    add(NODE["lora_a"], "lora_name", fa)
    if fa is not None:
        add(NODE["lora_a"], "strength_model", sa)
    add(NODE["lora_b"], "lora_name", fb)
    if fb is not None:
        add(NODE["lora_b"], "strength_model", sb)
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
    req = urllib.request.Request(file_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


def _do_run(webapp_id, mode, prompt_text, width, height, steps, denoise,
            ref_image_path, download_dir, on_submit, verbose,
            lora_name=None, lora_strength=None,
            lora_name_b=None, lora_strength_b=None):
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
                         lora_name=lora_name, lora_strength=lora_strength,
                         lora_name_b=lora_name_b, lora_strength_b=lora_strength_b)
    if verbose:
        print(f"[App] {webapp_id} ({mode}) 提交 {len(ni)} 个字段: "
              f"{', '.join(n['nodeId'] + '.' + n['fieldName'] for n in ni)}")

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
            on_submit=None, verbose=True):
    """文生图：只用提示词 + 尺寸"""
    return _do_run(APP_T2I, "文生图", prompt_text, width, height, steps, None,
                   None, download_dir, on_submit, verbose)


def run_i2i(prompt_text, ref_image_path, width=DEFAULT_W, height=DEFAULT_H,
            steps=DEFAULT_STEPS, denoise=DEFAULT_DENOISE,
            download_dir="outputs", on_submit=None, verbose=True,
            lora_name=None, lora_strength=None,
            lora_name_b=None, lora_strength_b=None):
    """
    图生图：参考图 + 提示词 + 重绘强度 + 可选 LoRA

    lora_name          节点 14 的 LoRA 文件名（默认 Kook_Zimage_瑶光.safetensors）
    lora_strength      节点 14 的强度（默认 0.3）
    lora_name_b        节点 20 的 LoRA 文件名（默认 Kook_Zimage_如梦似幻.safetensors）
    lora_strength_b    节点 20 的强度（默认 0.25）

    只换一个位子时，另一个位子传 "" 可关闭该 LoRA。
    """
    if not ref_image_path:
        raise ValueError("图生图需要 ref_image_path")
    return _do_run(APP_I2I, "图生图", prompt_text, width, height, steps, denoise,
                   ref_image_path, download_dir, on_submit, verbose,
                   lora_name=lora_name, lora_strength=lora_strength,
                   lora_name_b=lora_name_b, lora_strength_b=lora_strength_b)


def run(prompt_text, ref_image_path=None, width=DEFAULT_W, height=DEFAULT_H,
        steps=None, denoise=None, download_dir="outputs",
        on_submit=None, verbose=True,
        lora_name=None, lora_strength=None,
        lora_name_b=None, lora_strength_b=None, **_ignored):
    """
    统一入口（兼容 rh_webui 旧调用签名）：
      有 ref_image_path → 图生图（支持 LoRA 运行时替换）
      无 ref_image_path → 文生图（无 LoRA 节点，相关参数忽略）

    lora / lora_strength 旧参数名仍被接受（走 **_ignored），
    但实际请用 lora_name / lora_strength。
    """
    if ref_image_path:
        return run_i2i(
            prompt_text, ref_image_path, width=width, height=height,
            steps=steps if steps is not None else DEFAULT_STEPS,
            denoise=denoise if denoise is not None else DEFAULT_DENOISE,
            download_dir=download_dir, on_submit=on_submit, verbose=verbose,
            lora_name=lora_name, lora_strength=lora_strength,
            lora_name_b=lora_name_b, lora_strength_b=lora_strength_b,
        )
    return run_t2i(
        prompt_text, width=width, height=height,
        steps=steps if steps is not None else DEFAULT_STEPS,
        download_dir=download_dir, on_submit=on_submit, verbose=verbose,
    )


# ==================== 自检 ====================
IS_APP_CHANNEL = True      # 通知 rh_webui：pid 是 App taskId，不是 /proxy 的 prompt_id
CODE_NOT_FOUND = 807       # APIKEY_TASK_NOT_FOUND：任务号不存在/已过期

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
    返回 {'status': success/error/running/queued/not_found/api_error, 'detail': ...}
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
        return {"status": "not_found", "detail": d.get("msg") or "远端无此任务"}
    if code == 301:
        # 301 是复用码：按 msg 分流，taskId 脏数据判 not_found，鉴权问题判 api_error
        msg = (d.get("msg") or "").lower()
        if "taskid" in msg:
            return {"status": "not_found", "detail": d.get("msg") or "任务号无效"}
        return {"status": "api_error", "detail": "API Key 无效或未配置：301 " + (d.get("msg") or "")}
    if code == -1:
        return {"status": "api_error", "detail": d.get("msg") or "网络异常"}
    return {"status": "api_error", "detail": f"未知返回码 {code}: {d.get('msg') or ''}"}

def list_loras():
    """打印可用 LoRA 别名（运行时可直接把短名传给 lora_name）"""
    lines = ["=== LoRA 别名（传短名即可，会自动展开成真实文件名）==="]
    for k, (fn, st) in LORA_ALIAS.items():
        lines.append(f"  {k:10} @{st:<5} {fn}")
    lines.append("")
    lines.append("也可以直接传任意真实文件名，只要它在平台 loras 目录里。")
    return "\n".join(lines)


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
