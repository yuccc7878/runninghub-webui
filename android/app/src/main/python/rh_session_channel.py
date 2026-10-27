#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rh_session_channel.py —— 网页版 session 通道（替代 openapi 通道）

背景
    /task/openapi/* 属于 API Key 专用通道，平台不计入创作者收益与应用运行次数。
    网页端 /task/webapp/* 走 session 鉴权，会计入。

    实测对比（2026-10-28，zimage 文生图 App 2112630365772128258）：
        openapi 通道  POST /task/openapi/ai-app/run    计数不动
        session 通道  POST /task/webapp/create         计数 70 → 71 ✅

设计
    不改动 11 个业务模块，而是在注入阶段替换它们的 _post，
    把 openapi 请求翻译成 session 请求。业务模块零感知。

映射关系
    /task/openapi/ai-app/run   ->  /task/webapp/create
        {webappId, apiKey, nodeInfoList}  ->  {webappId, inputs, clientId, usePersonalQueue}
        nodeInfoList 元素缺 nodeName，从 /api/webapp/detail 补齐

    /task/openapi/outputs      ->  /api/output/taskHistory
        服务端忽略 taskId 参数，必须拉列表后本地按 taskId 过滤（实测）
        列表有 2~3 分钟缓存延迟，未命中返回 RUNNING 而非报错

    /api/webapp/detail         不变，但改用 session 鉴权

鉴权三件套（缺一不可）
    Authorization: <Rh-Accesstoken>
    Rh-Identify:   <32位>
    Rh-Comfy-Auth: <180位，仅上传接口需要>

配置
    workflows/session.json  {"session": {token, identify, comfy, userId, expire, name}}
"""
import json
import os
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(os.environ.get("RH_BASE") or Path(__file__).resolve().parent.parent)
CONF_FILE = BASE_DIR / "workflows" / "session.json"
BASE = "https://www.runninghub.ai"

_lock = threading.Lock()
_detail_cache = {}          # webappId -> (ts, data)
_DETAIL_TTL = 300


# ==================== 配置读写 ====================

def load_conf():
    try:
        if CONF_FILE.exists():
            return json.loads(CONF_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_conf(cfg):
    CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONF_FILE)


def get_session():
    s = load_conf().get("session") or {}
    return {
        "token": s.get("token") or "",
        "refresh": s.get("refresh") or "",
        "identify": s.get("identify") or "",
        "comfy": s.get("comfy") or "",
        "comfyExpire": s.get("comfyExpire") or 0,
        "expire": s.get("expire") or 0,
        "userId": s.get("userId") or "",
        "name": s.get("name") or "",
        "email": s.get("email") or "",
    }


def get_client_id():
    """clientId 持久化，模拟网页端稳定设备标识。"""
    with _lock:
        cfg = load_conf()
        cid = cfg.get("clientId")
        if not cid:
            import uuid
            cid = uuid.uuid4().hex
            cfg["clientId"] = cid
            save_conf(cfg)
        return cid


def is_configured():
    return bool(get_session()["token"])


# ==================== 账号登录 / 令牌刷新 / 凭证重签 ====================

def _md5(s):
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def parse_jwt(token):
    """解析 JWT payload（不验签）。返回 dict 或 {}。

    实测坑：网页 localStorage.userId 可能是上一个账号的脏值，
    真实 userId 必须从 token 的 sub 取。
    """
    try:
        import base64
        parts = str(token).split(".")
        if len(parts) < 2:
            return {}
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(seg).decode("utf-8", "replace")
        return json.loads(raw)
    except Exception:
        return {}


def _raw_post_absolute(path, body, headers, timeout=30):
    """独立于会话的裸请求，用于登录/刷新（此时还没有 token）。"""
    import urllib.request
    import urllib.error
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    try:
        txt = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        return json.loads(txt)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"code": e.code, "msg": "HTTP %s" % e.code}
    except Exception as e:
        return {"code": -1, "msg": "%s: %s" % (type(e).__name__, e)}


def login(email, password, timeout=30):
    """账号密码登录。

    实测（2026-10-28）：
      POST /uc/abroad/pwdLogin  {"email":..., "password": MD5(明文)}
      密码只做一次 MD5，无盐无 RSA。
      返回 data 含 access_token / refresh_token / identify / expire_in
      注意 expire_in 是**相对毫秒数**，需加 Date.now()。
    """
    email = (email or "").strip()
    if not email or not password:
        return {"ok": False, "error": "账号和密码都不能为空"}

    # 带 @ 视为邮箱走 abroad 通道，否则按手机号走国内通道
    if "@" in email:
        path = "/uc/abroad/pwdLogin"
        payload = {"email": email, "password": _md5(password)}
    else:
        path = "/uc/pwdLogin"
        payload = {"mobile": email, "password": _md5(password)}

    d = _raw_post_absolute(path, payload, {"Content-Type": "application/json"}, timeout)
    if d.get("code") != 0:
        msg = d.get("msg") or d.get("errorMessages") or "登录失败"
        return {"ok": False, "error": "code=%s %s" % (d.get("code"), msg), "raw": d}

    data = d.get("data") or {}
    at = data.get("access_token") or ""
    rt = data.get("refresh_token") or ""
    idf = data.get("identify") or ""
    if not at:
        return {"ok": False, "error": "登录响应缺少 access_token: %s" % str(data)[:300]}

    claims = parse_jwt(at)
    uid = str(claims.get("sub") or "")
    expire_ms = int(time.time() * 1000) + int(data.get("expire_in") or 0)

    sess = {
        "token": at,
        "refresh": rt,
        "identify": idf,
        "comfy": "",
        "comfyExpire": 0,
        "userId": uid,
        "name": data.get("nickName") or email,
        "email": email,
        "expire": expire_ms,
        "loginAt": int(time.time()),
    }
    with _lock:
        cfg = load_conf()
        cfg["session"] = sess
        save_conf(cfg)
    _detail_cache.clear()

    # 立刻签一次 comfy 凭证（上传需要），失败不影响登录结果
    _sign_comfy_auth(quiet=True)
    return {"ok": True, "session": get_session()}


def refresh_token(timeout=30):
    """用 refresh_token 换新 access_token。

    实测：POST /uc/token/refresh，refresh token 放 Authorization 头，body 空。
    返回新 access_token + 新 refresh_token（滑动过期，可长期续）。
    """
    s = get_session()
    rt = s.get("refresh") or ""
    if not rt:
        return {"ok": False, "error": "没有 refresh_token，需要重新登录"}

    d = _raw_post_absolute("/uc/token/refresh", {},
                           {"Content-Type": "application/json",
                            "Authorization": rt}, timeout)
    if d.get("code") != 0:
        msg = d.get("msg") or "刷新失败"
        return {"ok": False, "error": "code=%s %s" % (d.get("code"), msg), "raw": d}

    data = d.get("data") or {}
    at = data.get("access_token") or ""
    if not at:
        return {"ok": False, "error": "刷新响应缺少 access_token"}

    claims = parse_jwt(at)
    with _lock:
        cfg = load_conf()
        cur = cfg.get("session") or {}
        cur["token"] = at
        if data.get("refresh_token"):
            cur["refresh"] = data["refresh_token"]
        if data.get("identify"):
            cur["identify"] = data["identify"]
        if claims.get("sub"):
            cur["userId"] = str(claims["sub"])
        if data.get("expire_in"):
            cur["expire"] = int(time.time() * 1000) + int(data["expire_in"])
        cur["refreshAt"] = int(time.time())
        cfg["session"] = cur
        save_conf(cfg)
    return {"ok": True, "session": get_session()}


def _sign_comfy_auth(quiet=False, timeout=30):
    """重新签发 Rh-Comfy-Auth（上传 image 必须）。

    实测：POST /api/instance/access/auth（Authorization = access_token）
    返回 {accessKey, expire_in}，accessKey 结构 = {userId, signExpire, ts, sign}，
    与网页 localStorage 的 Rh-Comfy-Auth 同构。
    """
    s = get_session()
    if not s["token"]:
        return {"ok": False, "error": "未登录"}
    d = _raw_post_absolute("/api/instance/access/auth", {},
                           {"Content-Type": "application/json",
                            "Authorization": s["token"]}, timeout)
    if d.get("code") != 0:
        if not quiet:
            return {"ok": False, "error": "code=%s %s" % (d.get("code"), d.get("msg"))}
        return {"ok": False, "error": "sign failed"}
    data = d.get("data") or {}
    key = data.get("accessKey") or ""
    if not key:
        return {"ok": False, "error": "未返回 accessKey"}
    exp = int(data.get("expire_in") or 0)
    with _lock:
        cfg = load_conf()
        cur = cfg.get("session") or {}
        cur["comfy"] = key
        cur["comfyExpire"] = exp
        cfg["session"] = cur
        save_conf(cfg)
    return {"ok": True, "comfy": key, "expire": exp}


def ensure_fresh(margin_ms=120000):
    """每次请求前调用：临近过期就刷新，comfy 凭证缺失/过期就重签。

    margin_ms 默认提前 2 分钟刷新，避免请求发出时正好失效。
    """
    s = get_session()
    if not s["token"]:
        return {"ok": False, "error": "未登录"}

    changed = False
    now = int(time.time() * 1000)

    # access token 快过期 -> 刷新
    try:
        exp = int(s.get("expire") or 0)
    except Exception:
        exp = 0
    if exp and exp - now < margin_ms:
        r = refresh_token()
        if not r["ok"]:
            return {"ok": False, "error": "token 已过期且刷新失败：%s" % r.get("error"),
                    "need_login": True}
        changed = True

    # comfy 凭证缺失/过期 -> 重签
    s2 = get_session()
    try:
        cexp = int(s2.get("comfyExpire") or 0)
    except Exception:
        cexp = 0
    if not s2.get("comfy") or (cexp and cexp - now < margin_ms):
        r = _sign_comfy_auth(quiet=True)
        if r.get("ok"):
            changed = True

    return {"ok": True, "changed": changed}


def logout():
    with _lock:
        cfg = load_conf()
        cfg.pop("session", None)
        save_conf(cfg)
    _detail_cache.clear()
    return {"ok": True}


def is_expired():
    exp = get_session().get("expire") or 0
    try:
        exp = int(exp)
    except Exception:
        return False
    return bool(exp) and exp < int(time.time() * 1000)


def status():
    s = get_session()
    claims = parse_jwt(s["token"]) if s["token"] else {}
    return {
        "configured": bool(s["token"]),
        "expired": is_expired(),
        "expire": s["expire"],
        "userId": s["userId"] or str(claims.get("sub") or ""),
        "name": s["name"],
        "email": s["email"],
        "hasRefresh": bool(s.get("refresh")),
        "comfyReady": bool(s.get("comfy")),
        "comfyExpire": s.get("comfyExpire") or 0,
        "tokenHead": (s["token"][:16] + "...") if s["token"] else "",
    }


# ==================== 底层请求 ====================

def _headers(with_comfy=False):
    s = get_session()
    h = {"Authorization": s["token"], "Content-Type": "application/json"}
    if with_comfy:
        h["Rh-Identify"] = s["identify"]
        h["Rh-Comfy-Auth"] = s["comfy"]
    return h


def raw_post(path, body, timeout=40, with_comfy=False):
    """返回 (http_ok, parsed_json)。仅内部使用。

    每次调用前自动 ensure_fresh：token 将过期就刷新，comfy 凭证缺失就重签。
    """
    # 先保活（失败不阻断，真正鉴权失败会由下方请求暴露出来）
    try:
        if get_session().get("token"):
            ensure_fresh()
    except Exception:
        pass

    s = get_session()
    if not s["token"]:
        return False, {"code": -1, "msg": "未配置会话 token"}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b"{}"
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    for k, v in _headers(with_comfy).items():
        req.add_header(k, v)
    try:
        txt = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        return True, json.loads(txt)
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return False, {"code": e.code, "msg": "HTTP %s" % e.code}
    except Exception as e:
        return False, {"code": -1, "msg": "%s: %s" % (type(e).__name__, e)}


def api_post(path, body, timeout=40, with_comfy=False):
    """统一返回 {ok, data|error}。"""
    ok, d = raw_post(path, body, timeout, with_comfy)
    if not ok and d.get("code") is None:
        return {"ok": False, "error": d.get("msg") or "请求失败"}
    if d.get("code") == 0:
        return {"ok": True, "data": d.get("data")}
    return {"ok": False, "error": "code=%s msg=%s" % (d.get("code"), d.get("msg")),
            "raw": d}


# ==================== App 详情（带缓存） ====================

def webapp_detail(webapp_id, force=False):
    """取 App 详情。带 5 分钟进程内缓存，避免补 nodeName 时反复请求。"""
    key = str(webapp_id)
    now = time.time()
    if not force:
        cached = _detail_cache.get(key)
        if cached and now - cached[0] < _DETAIL_TTL:
            return cached[1]
    r = api_post("/api/webapp/detail", {"webappId": key}, timeout=20)
    if not r["ok"]:
        return {}
    data = r.get("data") or {}
    _detail_cache[key] = (now, data)
    return data


def field_index(webapp_id):
    """返回 {(nodeId, fieldName): {nodeName, fieldType}}，用于补齐 nodeName。"""
    data = webapp_detail(webapp_id)
    idx = {}
    for n in (data.get("inputNodes") or []):
        idx[(str(n.get("nodeId")), n.get("fieldName"))] = {
            "nodeName": n.get("nodeName"),
            "fieldType": n.get("fieldType"),
        }
    return idx


# ==================== 核心：openapi -> session 翻译 ====================

def _build_inputs(webapp_id, node_info_list):
    """nodeInfoList -> inputs。缺 nodeName 的从 detail 补，补不到则用 nodeId 兜底。"""
    idx = field_index(webapp_id)
    out = []
    for item in (node_info_list or []):
        nid = str(item.get("nodeId"))
        fname = item.get("fieldName")
        fval = item.get("fieldValue")
        meta = idx.get((nid, fname)) or {}
        node_name = item.get("nodeName") or meta.get("nodeName") or ("node" + nid)
        out.append({
            "nodeId": nid,
            "nodeName": node_name,
            "fieldName": fname,
            "fieldValue": "" if fval is None else str(fval),
            "description": item.get("description") or fname or "",
        })
    return out


def create_task(webapp_id, node_info_list, use_personal_queue=False, timeout=60):
    """对应 openapi /task/openapi/ai-app/run。返回 (taskId, raw)。"""
    inputs = _build_inputs(webapp_id, node_info_list)
    body = {
        "webappId": str(webapp_id),
        "inputs": inputs,
        "clientId": get_client_id(),
        "usePersonalQueue": bool(use_personal_queue),
    }
    ok, d = raw_post("/task/webapp/create", body, timeout=timeout)
    if not ok and d.get("code") is None:
        raise RuntimeError("提交失败: %s" % d.get("msg"))
    if d.get("code") != 0:
        raise RuntimeError("提交失败: %s" % json.dumps(d, ensure_ascii=False)[:400])
    data = d.get("data") or {}
    tid = data.get("taskId") or data.get("id")
    if not tid:
        raise RuntimeError("未返回 taskId: %s" % json.dumps(d, ensure_ascii=False)[:400])
    return str(tid), data


# openapi 状态码 -> 语义（供 poll 复用）
_ST_MAP = {
    "SUCCESS": ("SUCCESS", 0),
    "RUNNING": ("RUNNING", 804),
    "QUEUED": ("RUNNING", 813),
    "PENDING": ("RUNNING", 813),
    "FAILED": ("FAILED", 805),
    "ERROR": ("FAILED", 805),
    "CANCELED": ("FAILED", 805),
}


def query_outputs(task_id, webapp_id=None, timeout=30):
    """
    对应 openapi /task/openapi/outputs。
    实测要点：
      1. taskId 参数被服务端忽略，必须拉列表后本地过滤
      2. 新任务有 2~3 分钟列表延迟，未命中返回 pending（不是错误）
    返回 {taskId, status, code, pending, costTime, outputs[], createTime}
    """
    task_id = str(task_id)
    ok, d = raw_post("/api/output/taskHistory", {"size": 40, "current": 1}, timeout=timeout)
    if not ok and d.get("code") is None:
        return {"taskId": task_id, "status": "ERROR", "code": -1, "pending": False,
                "outputs": [], "error": d.get("msg")}
    recs = ((d.get("data") or {}).get("records") or [])
    rec = next((x for x in recs if str(x.get("taskId")) == task_id), None)
    if rec is None:
        # 未命中：默认按运行中处理，由调用方超时兜底
        return {"taskId": task_id, "status": "RUNNING", "code": 813,
                "pending": True, "costTime": None, "createTime": None,
                "outputs": [], "note": "列表未同步（约 2~3 分钟延迟）"}
    raw_st = str(rec.get("taskStatus") or "").upper()
    st, code = _ST_MAP.get(raw_st, ("RUNNING", 813))
    outs = []
    for o in (rec.get("outputList") or []):
        outs.append({
            "fileUrl": o.get("fileUrl"),
            "fileType": o.get("outputType"),
            "outputName": o.get("outputName"),
            "outputSize": o.get("outputSize"),
            "expireTime": o.get("expireTime"),
            # 兼容旧模块可能读的字段名
            "name": o.get("outputName"),
            "type": o.get("outputType"),
            "url": o.get("fileUrl"),
        })
    return {
        "taskId": rec.get("taskId"),
        "status": st,
        "rawStatus": raw_st,
        "code": code,
        "pending": False,
        "costTime": rec.get("taskCostTime"),
        "createTime": rec.get("createTime"),
        "outputs": outs,
    }


def list_tasks(size=30, timeout=30):
    ok, d = raw_post("/api/output/taskHistory", {"size": int(size), "current": 1}, timeout=timeout)
    if not ok and d.get("code") is None:
        return {"ok": False, "error": d.get("msg")}
    data = d.get("data") or {}
    recs = []
    for r in (data.get("records") or []):
        outs = [{
            "name": o.get("outputName"), "type": o.get("outputType"),
            "url": o.get("fileUrl"), "expire": o.get("expireTime"),
        } for o in (r.get("outputList") or [])]
        recs.append({
            "taskId": r.get("taskId"), "status": r.get("taskStatus"),
            "costTime": r.get("taskCostTime"), "createTime": r.get("createTime"),
            "outputs": outs,
        })
    return {"ok": True, "total": data.get("total"), "records": recs}


def estimate_price(webapp_id, node_info_list, timeout=30):
    """对应 /api/workflow/webapp-price-estimate。实测该接口返回 0，仅作参考。"""
    inputs = _build_inputs(webapp_id, node_info_list)
    s = get_session()
    uid = s.get("userId")
    ok, d = raw_post("/api/workflow/webapp-price-estimate",
                     {"webappId": str(webapp_id), "inputs": inputs}, timeout=timeout)
    if d.get("code") == 0:
        return {"ok": True, "data": d.get("data"), "userId": uid}
    return {"ok": False, "error": "code=%s msg=%s" % (d.get("code"), d.get("msg")), "data": None}


def upload_image(filepath, timeout=120):
    """
    图片上传，必须三件套齐全。返回 {ok, name, url}
    实测缺 Rh-Identify / Rh-Comfy-Auth 任一即 401。
    """
    s = get_session()
    if not s["token"]:
        return {"ok": False, "error": "未配置会话"}
    p = Path(filepath)
    if not p.exists():
        return {"ok": False, "error": "文件不存在: %s" % filepath}
    boundary = "----RhForm" + str(int(time.time() * 1000))
    ctype = "png" if p.suffix.lower() == ".png" else (
        "jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "octet-stream")
    body = b""
    body += ("--" + boundary + "\r\n").encode()
    body += ('Content-Disposition: form-data; name="image"; filename="%s"\r\n' % p.name).encode()
    body += ("Content-Type: image/%s\r\n\r\n" % ctype).encode()
    body += p.read_bytes()
    body += ("\r\n--" + boundary + "--\r\n").encode()
    req = urllib.request.Request(BASE + "/upload/image", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("Authorization", s["token"])
    req.add_header("Rh-Identify", s["identify"])
    req.add_header("Rh-Comfy-Auth", s["comfy"])
    try:
        txt = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        d = json.loads(txt)
        if d.get("name"):
            return {"ok": True, "name": d.get("name"), "subfolder": d.get("subfolder"),
                    "type": d.get("type")}
        return {"ok": False, "error": json.dumps(d, ensure_ascii=False)[:300]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
