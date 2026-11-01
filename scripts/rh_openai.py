#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub OpenAI 兼容层（v1 一期：zimage 文生图）
====================================================
端点：
    GET  /v1/models                      模型列表（含 server_base 局域网发现）
    POST /v1/images/generations          文生图（同步默认 / async 扩展异步）
    GET  /v1/images/generations/<task>   异步任务轮询
    GET  /v1/files/<name>                产物静态访问

设计：
    - 强制鉴权：Authorization: Bearer <openai_key>（存 workflows/api_key.json）
    - 同步 = 直接调 rh_zimage_api.run()（阻塞式，ThreadingHTTPServer 多线程下安全）
    - 异步 = 后台线程跑同一个 run()，任务表内存态
    - 产物落 WebUI 的 webui_out/，与网页出图共用历史记录
    - url 跟请求 Host 头动态生成：同设备 127.0.0.1 / 同内网 192.168.x.x 自动适配
    - 只提供出图；不代理对话（避免把用户私有 LLM Key 借给别人）
"""
import json
import os
import re
import sys
import time
import socket
import base64
import uuid
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# 注意：下面两个常量只是「本地兜底」。
# 安卓端 serve.py 会把 rh_webui.BASE / OUT_DIR 改到 filesDir（可写），
# 而模块级算出来的 BASE 在 Chaquopy 下指向 AssetFinder（只读、且没有 api_key.json）。
# 因此配置路径与产物目录必须运行期从 rh_webui 取，不能在这里固化。
CONFIG_FILE = BASE / "workflows" / "api_key.json"
OUT_DIR = BASE / "workflows" / "openai_out"
# 产物统一写进 WebUI 的产物目录（webui_out），历史记录才能共用一套磁盘校验。
# script 键：API 出图在历史里的分组标识（前端有对应中文标签）。
API_SCRIPT = "api"


def _webui():
    """取 rh_webui 模块。运行期一定有（路由由它挂载），避免循环导入。

    两种启动方式模块名不同，必须都认：
      - 安卓 Chaquopy：serve.py 里 `import rh_webui` → sys.modules['rh_webui']
      - 命令行/本地：`python3 scripts/rh_webui.py` → sys.modules['__main__']
    只查 'rh_webui' 会漏掉后者，导致目录静默回退到模块级兜底路径。
    """
    w = sys.modules.get("rh_webui")
    if w is not None:
        return w
    m = sys.modules.get("__main__")
    # 用 TASKS + OUT_DIR 双重特征确认就是 rh_webui，避免误判成别的入口脚本
    if m is not None and hasattr(m, "TASKS") and hasattr(m, "OUT_DIR"):
        return m
    return None


def _out_dir():
    """产物目录：跟随 rh_webui.OUT_DIR，保证 API 出的图和网页出的图在同一处。"""
    w = _webui()
    d = getattr(w, "OUT_DIR", None) if w else None
    return Path(d) if d else OUT_DIR


def _cfg_file():
    """配置文件路径：跟随 rh_webui.BASE（安卓下已被 serve.py 指到 filesDir）。"""
    w = _webui()
    b = getattr(w, "BASE", None) if w else None
    return (Path(b) / "workflows" / "api_key.json") if b else CONFIG_FILE


MODEL = "zimage"

# 可用的出图模型。全部走 App 通道（账号认证，不需要 API Key）。
# 每个模型的字段能力不同，见各自定义。
MODEL_SPECS = {
    "zimage": {
        "module": "rh_zimage_app",
        "entry": "run_t2i",
        "app": "2112630365772128258",
        # 暴露 5.text / 16.value(宽) / 17.value(高) / 4.steps
        "supports": {"width": True, "height": True, "steps": True},
        "needs_ratio": False,
        "label": "Zimage Turbo 文生图",
    },
    "krea2": {
        "module": "rh_krea2_app",
        "entry": "run_t2i",
        "app": "2113009215693500417",
        # 暴露 41.text / 39.value(宽) / 40.value(高) / 36.steps + 3 组 LoRA 强度
        "supports": {"width": True, "height": True, "steps": True},
        "needs_ratio": False,
        "label": "Krea2 文生图",
    },
    "moss": {
        "module": "rh_moss_app",
        "entry": "run",
        "app": "2114331355738923010",
        # App 只暴露 4.text / 23.aspect_ratio / 16.steps。
        # 但实测 6.EmptyFlux2LatentImage 的 width/height 可直传且优先级高于 ratio，
        # 所以 size 能精确控制（不再受 8 档比例枚举限制）。
        "supports": {"width": True, "height": True, "steps": True},
        "needs_ratio": False,
        "label": "MOSS 八步加速版",
    },
}

MODEL_ALIASES = {
    "zimage", "zimage-t2i", "zimage_t2i",
    "krea2", "krea2-t2i", "krea2_t2i",
    "moss", "moss-t2i", "moss_t2i",
}

# 别名 → 规范名
MODEL_CANON = {
    "zimage": "zimage", "zimage-t2i": "zimage", "zimage_t2i": "zimage",
    "krea2": "krea2", "krea2-t2i": "krea2", "krea2_t2i": "krea2",
    "moss": "moss", "moss-t2i": "moss", "moss_t2i": "moss",
}

# MOSS 的比例枚举（App 只给这 8 个）
MOSS_RATIOS = [
    "1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)",
    "3:4 (Portrait Standard)", "4:3 (Standard)",
    "9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "21:9 (Ultrawide)",
]

# zimage 支持的画布档位（面积最近匹配）
ZIMAGE_SIZES = {
    "768x1368": (768, 1368),
    "1080x1920": (1080, 1920),
    "1242x2688": (1242, 2688),
    "1024x1024": (1024, 1024),
    "1368x768": (1368, 768),
    "1920x1080": (1920, 1080),
}

_TASKS = {}
_TASKS_LOCK = threading.Lock()


def _load_cfg():
    try:
        return json.loads(_cfg_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_openai_key():
    return _load_cfg().get("openai_key") or ""


def _hget(headers, name):
    """大小写不敏感的 header 取值。
    注意：调用方把 self.headers 转成了普通 dict，丢了 email.Message 的大小写不敏感特性，
    部分 HTTP 客户端会发全小写头名（authorization），精确匹配会漏。"""
    name = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == name:
            return str(v)
    return ""


def check_auth(headers):
    """校验 Bearer。返回 (ok, message)"""
    key = get_openai_key()
    if not key:
        return False, "OpenAI 兼容层未配置 openai_key（workflows/api_key.json 中设置后重启生效）"
    raws = [
        _hget(headers, "authorization"),
        _hget(headers, "api-key"),
        _hget(headers, "x-api-key"),
    ]
    for raw in raws:
        if not raw:
            continue
        v = raw.strip()
        if v.lower().startswith("bearer "):
            v = v[7:].strip()
        # 有些客户端会带引号
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].strip()
        if v == key:
            return True, ""
    # 诊断日志：打印收到的鉴权头（脱敏）+ 长度/首个差异位置，便于定位客户端填错
    try:
        hdrs = {k: v for k, v in (headers or {}).items()
                if k.lower() in ("authorization", "api-key", "x-api-key")}
        shown = {}
        for k, v in hdrs.items():
            v = str(v)
            tok = v.split(" ", 1)[1] if " " in v else v
            tok = tok.strip()
            shown[k] = (f"{tok[:10]}****{tok[-4:]} (len={len(tok)})"
                        if len(tok) > 14 else f"{tok[:4]}**** (len={len(tok)})")
        # 首个差异位置
        diff = -1
        for i, (a, b) in enumerate(zip(
                str(list(hdrs.values())[0] if hdrs else "").split(" ")[-1].strip(), key)):
            if a != b:
                diff = i
                break
        print(f"[auth-fail] 收到: {shown or '无鉴权头'} | 期望: {key[:10]}****{key[-4:]} "
              f"(len={len(key)}) | 首个差异位={diff}", flush=True)
    except Exception:
        pass
    return False, "Invalid API key"


def list_lan_ips():
    """枚举本机非回环 IPv4（局域网发现用）"""
    ips = []
    try:
        import subprocess
        out = subprocess.check_output(["ip", "-4", "addr"], stderr=subprocess.DEVNULL).decode(errors="ignore")
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", out):
            ip = m.group(1)
            if not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def models(port):
    ips = list_lan_ips()
    data = []
    for mid, spec in MODEL_SPECS.items():
        caps = []
        if spec["supports"]["width"]:
            caps.append("size")
        else:
            caps.append("ratio(由 size 折算)")
        if spec["supports"]["steps"]:
            caps.append("steps")
        data.append({
            "id": mid,
            "object": "model",
            "created": 1735689600,
            "owned_by": "runninghub",
            "description": "%s（App %s；支持：%s）" % (
                spec["label"], spec["app"], "、".join(caps)),
        })
    return {
        "object": "list",
        "server_base": {
            "local": f"http://127.0.0.1:{port}/v1",
            "lan": [f"http://{ip}:{port}/v1" for ip in ips],
        },
        "data": data,
    }


def parse_size(size):
    """OpenAI size → (width, height)。精确匹配档位，否则按面积最近 + 宽高比方向。"""
    if not size:
        return 1080, 1920
    size = str(size).strip().lower().replace("×", "x")
    if size in ZIMAGE_SIZES:
        return ZIMAGE_SIZES[size]
    if "x" in size:
        try:
            w, h = map(int, size.split("x")[:2])
            # 先按宽高比方向过滤（竖/横/方），再面积最近
            req_ratio = w / h if h else 0
            candidates = [(kw, kh) for kw, kh in ZIMAGE_SIZES.values()]
            # 方图优先匹配 1:1
            if abs(req_ratio - 1.0) < 0.15:
                candidates = [(kw, kh) for kw, kh in candidates if abs(kw / kh - 1.0) < 0.15] or candidates
            elif req_ratio < 1.0:  # 竖屏
                candidates = [(kw, kh) for kw, kh in candidates if kw < kh] or candidates
            else:  # 横屏
                candidates = [(kw, kh) for kw, kh in candidates if kw > kh] or candidates
            area = w * h
            best = min(candidates, key=lambda c: abs(c[0] * c[1] - area))
            return best
        except Exception:
            pass
    return 1080, 1920


def _inject_key(mod):
    """注入 RH API Key 到目标模块（对齐 rh_webui._inject_api_key）。
    关键：API_KEY 改了还不够，PROXY 是 import 时用旧 key 拼好的字符串，必须一起重建。
    """
    try:
        # API 通道已断开时不再注入 key（绕开配置文件的读取）
        try:
            import rh_webui as _W
            if getattr(_W, "API_CHANNEL_DISABLED", False):
                return
        except Exception:
            pass
        key = _load_cfg().get("api_key") or ""
        if not key:
            return
        if hasattr(mod, "API_KEY"):
            mod.API_KEY = key
        if hasattr(mod, "PROXY"):
            base = getattr(mod, "BASE", "https://www.runninghub.ai")
            mod.PROXY = f"{base}/proxy/{key}"
    except Exception:
        pass


# ===== 出图通道开关 =====
# False = 走 rh_zimage_api.run（ComfyUI 原生端点，全参数：steps/seed/denoise/lora）
#         —— 已改造为会话三件套认证，不需要 API Key，能力不打折
# True  = 走 rh_zimage_app.run_t2i（App 通道，只有 prompt + 尺寸，参数受限）
OPENAI_USE_APP_CHANNEL = True


def _unzip_first_image(zip_path):
    """把产物 zip 解出一张图，返回本地路径。失败则返回原 zip 路径。"""
    import zipfile
    zp = Path(str(zip_path))
    if not zp.exists():
        return zip_path
    try:
        with zipfile.ZipFile(zp) as z:
            imgs = [n for n in z.namelist()
                    if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
            if not imgs:
                return zip_path
            member = imgs[0]
            # 用 zip 主名做唯一名，避免不同 zip 内同名成员互相覆盖
            out = zp.with_suffix(Path(member).suffix)
            if out.exists():
                import time as _t
                out = zp.parent / (zp.stem + "_" + str(int(_t.time())) + Path(member).suffix)
            with z.open(member) as src:
                out.write_bytes(src.read())
            return str(out)
    except Exception:
        return zip_path


def size_to_ratio(width, height):
    """把像素尺寸折算成最接近的比例枚举（MOSS 用）。

    按 |w/h - 目标比例| 取最小误差；同误差取先出现的。
    """
    if not width or not height:
        return MOSS_RATIOS[0]
    target = float(width) / float(height)
    best, best_err = MOSS_RATIOS[0], None
    for opt in MOSS_RATIOS:
        try:
            a, b = opt.split(" ")[0].split(":")
            err = abs(float(a) / float(b) - target)
        except Exception:
            continue
        if best_err is None or err < best_err:
            best, best_err = opt, err
    return best


def _run_via_app(model, prompt, width, height, extra, out_dir):
    """按模型分发出图。全部走 App 通道，不需要 API Key。"""
    import importlib
    spec = MODEL_SPECS.get(model) or MODEL_SPECS[MODEL]
    mod = importlib.import_module(spec["module"])
    entry = getattr(mod, spec["entry"])

    steps = extra.get("steps")
    kw = {"download_dir": str(out_dir), "verbose": True}
    if steps and spec["supports"]["steps"]:
        kw["steps"] = int(steps)

    if spec["supports"]["width"]:
        kw["width"] = width
        kw["height"] = height
    if spec["needs_ratio"]:
        # 仅当模型不支持精确宽高时，才折算比例
        kw["ratio"] = extra.get("ratio") or size_to_ratio(width, height)
    return entry(prompt_text=prompt, **kw)


def _run_one(prompt, width, height, seed, extra, host, model=MODEL):
    """跑一张图，返回 (本地路径, url)。

    通道选择：
      OPENAI_USE_APP_CHANNEL=True  → rh_zimage_app.run_t2i（App 通道，账号认证）
      OPENAI_USE_APP_CHANNEL=False → rh_zimage_api.run（/proxy 通道，API Key）
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    out_dir = _out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    if OPENAI_USE_APP_CHANNEL:
        res = _run_via_app(model, prompt, width, height, extra, out_dir)
    else:
        import rh_zimage_api as Z
        _inject_key(Z)
        res = Z.run(
            prompt_text=prompt,
            width=width,
            height=height,
            seed=seed,
            download_dir=str(out_dir),
            **extra,
        )
    saved = (res or {}).get("saved") or []
    if not saved:
        raise RuntimeError("平台未返回产物文件")
    path = saved[0]

    # App 通道（如 zimage 文生图）直出 zip；OpenAI 规范要求返回图片，
    # 这里解包取第一张图，避免第三方客户端拿到无法渲染的 zip。
    if str(path).lower().endswith(".zip"):
        path = _unzip_first_image(path)
    name = os.path.basename(path)
    _register_history(prompt, width, height, seed, extra, name,
                      (res or {}).get("prompt_id"))
    return path, f"http://{host}/v1/files/{name}"


def _register_history(prompt, width, height, seed, extra, name, pid):
    """把 API 出的图登记进 WebUI 历史记录（同一个 TASKS 表 + webui_out）。
    登记失败不影响出图结果，静默跳过。"""
    try:
        w = _webui()
        fn = getattr(w, "_register_api_task", None)
        if not fn:
            return
        params = {"prompt": prompt, "resolution": f"{width}x{height}"}
        if seed is not None:
            params["seed"] = seed
        for k in ("steps", "denoise", "lora", "lora_strength"):
            if extra.get(k) is not None:
                params[k] = extra[k]
        fn(params=params, files=[name], image=name, pid=pid)
    except Exception as e:
        print(f"[v1] 历史登记失败（不影响出图）: {e}", flush=True)


def _run_sync(prompt, width, height, n, fmt, seed, extra, host, model=MODEL):
    """同步生成 n 张（串行），返回 (http_code, openai_response)"""
    created = int(time.time())
    data = []
    for i in range(n):
        s = seed + i if seed is not None else None
        try:
            path, url = _run_one(prompt, width, height, s, extra, host, model)
        except Exception as e:
            return 500, {"error": {"message": f"平台生成失败: {e}", "type": "server_error"}}
        if fmt == "b64_json":
            try:
                b64 = base64.b64encode(Path(path).read_bytes()).decode()
            except Exception as e:
                return 500, {"error": {"message": f"读取产物失败: {e}", "type": "server_error"}}
            data.append({"b64_json": b64})
        else:
            data.append({"url": url})
    return 200, {"created": created, "data": data}


def _worker(tid, prompt, width, height, n, fmt, seed, extra, host, model=MODEL):
    """异步后台线程：跑完写回任务表"""
    with _TASKS_LOCK:
        _TASKS[tid]["status"] = "running"
    try:
        code, resp = _run_sync(prompt, width, height, n, fmt, seed, extra, host, model)
        with _TASKS_LOCK:
            if code == 200:
                _TASKS[tid]["status"] = "done"
                _TASKS[tid]["data"] = resp["data"]
            else:
                _TASKS[tid]["status"] = "error"
                _TASKS[tid]["error"] = resp.get("error", {}).get("message")
    except Exception as e:
        with _TASKS_LOCK:
            _TASKS[tid]["status"] = "error"
            _TASKS[tid]["error"] = str(e)


def generate(body, headers, host):
    """POST /v1/images/generations 主入口。返回 (http_code, response)"""
    ok, msg = check_auth(headers)
    if not ok:
        return 401, {"error": {"message": msg, "type": "authentication_error"}}
    model = (body.get("model") or MODEL).strip()
    if model not in MODEL_ALIASES:
        avail = ", ".join(sorted(MODEL_SPECS.keys()))
        return 400, {"error": {
            "message": f"Unknown model '{model}'. Available: {avail}",
            "type": "invalid_request_error", "param": "model"}}
    # 别名归一（zimage-t2i → zimage）
    model = MODEL_CANON.get(model, model)
    prompt = body.get("prompt")
    if not prompt or not str(prompt).strip():
        return 400, {"error": {"message": "Missing required parameter: prompt", "type": "invalid_request_error", "param": "prompt"}}
    width, height = parse_size(body.get("size"))
    try:
        n = max(1, min(int(body.get("n") or 1), 8))
    except Exception:
        n = 1
    fmt = body.get("response_format") or "url"
    if fmt not in ("url", "b64_json"):
        return 400, {"error": {"message": f"Unsupported response_format '{fmt}'", "type": "invalid_request_error", "param": "response_format"}}
    try:
        seed = int(body.get("seed")) if body.get("seed") is not None else None
    except Exception:
        seed = None
    extra = {}
    for k in ("steps", "denoise", "lora", "lora_strength"):
        if body.get(k) is not None:
            extra[k] = body[k]
    if body.get("async"):
        tid = uuid.uuid4().hex[:12]
        with _TASKS_LOCK:
            _TASKS[tid] = {"status": "queued", "created": int(time.time()), "data": None, "error": None}
        threading.Thread(target=_worker,
                         args=(tid, prompt, width, height, n, fmt, seed, extra, host, model),
                         daemon=True).start()
        return 200, {"task_id": tid, "status": "queued"}
    return _run_sync(prompt, width, height, n, fmt, seed, extra, host, model)


def task_status(tid):
    """GET /v1/images/generations/<tid> 轮询"""
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if not t:
            return 404, {"error": {"message": "Task not found", "type": "invalid_request_error", "param": "task_id"}}
        resp = {"task_id": tid, "status": t.get("status")}
        if t.get("status") == "done":
            resp["data"] = t.get("data")
        if t.get("error"):
            resp["error"] = t["error"]
        return 200, resp


def chat_completions(raw_body, headers):
    """POST /v1/chat/completions：**不提供**。

    这里曾经做过上游 LLM 透传，但那是把「用户本机自己配的私有 LLM Key」
    借给任何持有 openai_key 的人用（同内网即可）—— 等于替别人付账。
    OpenAI 兼容层只负责出图，对话请让客户端直连自己的 LLM 服务。
    """
    ok, msg = check_auth(headers)
    if not ok:
        return 401, {"error": {"message": msg, "type": "authentication_error"}}
    return 404, {
        "error": {
            "message": ("This endpoint only serves image generation. "
                        "Use /v1/images/generations with model 'zimage'. "
                        "For chat, point your client at your own LLM provider."),
            "type": "invalid_request_error",
            "code": "endpoint_not_supported",
        }
    }
