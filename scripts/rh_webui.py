#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RunningHub 跑图 WebUI 服务端（页面在 webui.html）"""

import json, os, sys, time, threading, traceback, importlib, re, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path

# BASE 三路自适应（单源维护）：
#   1) RH_BASE 环境变量优先（本地测试/迁移期可指定数据目录）
#   2) 默认按 __file__ 推导（仓库根 = scripts/ 的上一级，本地 python3 直接跑）
#   3) 安卓端由 serve.py 在运行时覆写为 filesDir
BASE = Path(os.environ.get("RH_BASE") or Path(__file__).resolve().parent.parent)
SCRIPTS_DIR = BASE / "scripts"
# Chaquopy(Android) 下 __file__ 位于 AssetFinder/app/rh_webui.py，BASE=AssetFinder/ 并无
# scripts/ 子目录，脚本模块其实与 rh_webui.py 同目录。若仍把不存在的路径插入 sys.path，
# Chaquopy 的 importer 会在 import urllib.request(→http.client→socket→_socket C 扩展) 时
# 抛 FileNotFoundError，导致所有访问 RunningHub 外网的请求（点数/市场/跑图/润色）全部失败。
if not SCRIPTS_DIR.is_dir():
    SCRIPTS_DIR = Path(__file__).resolve().parent
REFS_DIR = BASE / "refs"
OUT_DIR = BASE / "workflows" / "webui_out"
HTML_FILE = BASE / "webui.html"
if SCRIPTS_DIR.is_dir():
    sys.path.insert(0, str(SCRIPTS_DIR))

PORT = 8765
_lock = threading.Lock()
TASKS = {}
TASK_SEQ = [0]
CONFIG_FILE = BASE / "workflows" / "api_key.json"


def _load_config():
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_config(cfg):
    try:
        BASE.joinpath("workflows").mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _get_api_key():
    return (_load_config().get("api_key") or "").strip()


def _query_balance():
    """查询 RunningHub 账户余额/点数（POST /uc/openapi/accountStatus）"""
    key = _get_api_key()
    if not key:
        return {"ok": False, "error": "未配置 API Key"}
    import urllib.request
    req = urllib.request.Request(
        "https://www.runninghub.ai/uc/openapi/accountStatus",
        data=json.dumps({"apikey": key}).encode(),
        method="POST",
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace"))
        if r.get("code") == 0:
            d = r.get("data") or {}
            return {"ok": True, "remain_coins": d.get("remainCoins"), "task_counts": d.get("currentTaskCounts"),
                    "remain_money": d.get("remainMoney"), "currency": d.get("currency"), "api_type": d.get("apiType")}
        return {"ok": False, "error": f"RunningHub 返回错误: {r.get('msg') or r.get('errorMessage')}"}
    except Exception as e:
        return {"ok": False, "error": f"查询失败: {e}"}


def _rh_web(method, path, body=None, timeout=15):
    """调 RunningHub 网页端接口（国际站 .ai，Bearer 鉴权）。返回 dict。"""
    import urllib.request
    import urllib.error
    key = _get_api_key()
    if not key:
        return {"ok": False, "error": "未配置 API Key"}
    req = urllib.request.Request("https://www.runninghub.ai" + path, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    if body is not None:
        req.data = json.dumps(body).encode()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace"))
        if r.get("code") == 0:
            return {"ok": True, "data": r.get("data")}
        return {"ok": False, "error": f"RunningHub 返回错误: {r.get('msg') or r.get('errorMessage')}"}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", errors="replace")).get("msg") or str(e.code)
        except Exception:
            msg = f"HTTP {e.code}"
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _market_list(page=1, size=12, tag=None, keyword=None, sort=None):
    """应用市场列表。"""
    body = {"size": size, "current": page}
    if tag:
        body["tags"] = [tag]
    if sort:
        body["sort"] = sort
    if keyword:
        # RunningHub /api/webapp/list 的搜索字段名是 search（实测 extra.keyword 等均无效）
        body["search"] = keyword
    r = _rh_web("POST", "/api/webapp/list", body, timeout=20)
    if not r["ok"]:
        return r
    d = r["data"] or {}
    records = []
    for it in (d.get("records") or []):
        cov = (it.get("covers") or [{}])[0].get("url") or (it.get("preview") or {}).get("url") or ""
        records.append({
            "id": it.get("id"), "name": it.get("name"),
            "intro": it.get("intro") or "",
            "cover": cov,
            "tags": [t.get("name") for t in (it.get("tags") or [])][:3],
            "use_count": (it.get("statisticsInfo") or {}).get("useCount"),
            "owner": (it.get("owner") or {}).get("name") or "",
            "liked": it.get("liked"),
        })
    return {"ok": True, "records": records, "total": d.get("total"), "pages": d.get("pages"), "current": d.get("current")}


def _market_tags():
    """分类树。"""
    r = _rh_web("POST", "/api/portal/tag/tree", {"rang": "WEBAPP"}, timeout=15)
    if not r["ok"]:
        return r
    return {"ok": True, "tags": [{"id": t.get("id"), "name": t.get("name")} for t in (r["data"] or [])]}


_MK_DETAIL_CACHE = {}  # webapp_id -> (timestamp, detail)


def _market_detail(webapp_id):
    """应用详情：输入节点 + curl 示例。带 5 分钟内存缓存（外部接口不稳定）。"""
    now = time.time()
    cached = _MK_DETAIL_CACHE.get(webapp_id)
    if cached and now - cached[0] < 300:
        return {"ok": True, "detail": cached[1]}
    r = _rh_web("POST", "/api/webapp/detail", {"webappId": webapp_id}, timeout=10)
    if not r["ok"]:
        return r
    d = r["data"] or {}
    nodes = []
    for n in (d.get("inputNodes") or []):
        ft = n.get("fieldType") or "STRING"
        if ft in ("IMAGE", "AUDIO", "VIDEO", "ZIP"):
            nodes.append({"k": n.get("fieldName"), "label": n.get("nodeName") + " · " + n.get("fieldName"),
                          "type": "ref", "def": "", "help": n.get("description") or "",
                          "fieldType": ft, "nodeId": n.get("nodeId")})
        elif ft == "LIST":
            nodes.append({"k": n.get("fieldName"), "label": n.get("nodeName") + " · " + n.get("fieldName"),
                          "type": "select", "options": n.get("fieldValue") or "", "def": n.get("fieldValue") or "",
                          "help": n.get("description") or "", "fieldType": ft, "nodeId": n.get("nodeId")})
        else:
            nodes.append({"k": n.get("fieldName"), "label": n.get("nodeName") + " · " + n.get("fieldName"),
                          "type": "text", "def": n.get("fieldValue") or "", "help": n.get("description") or "",
                          "fieldType": ft, "nodeId": n.get("nodeId")})
    detail = {
        "id": d.get("id"), "name": d.get("name"),
        "desc": (d.get("description") or "").replace("<p>", "").replace("</p>", "\n"),
        "nodes": nodes, "curl": d.get("curl") or "",
        "owner": (d.get("owner") or {}).get("name") or "",
        "use_count": (d.get("statisticsInfo") or {}).get("useCount"),
        "instance_type": d.get("instanceType") or "default",
        "covers": [c.get("url") for c in (d.get("covers") or [])],
    }
    _MK_DETAIL_CACHE[webapp_id] = (now, detail)
    return {"ok": True, "detail": detail}


def _market_submit(webapp_id, node_values, webhook=None, instance_type="default"):
    """用 nodeInfoList 提交市场应用任务。node_values: {fieldName: value}"""
    key = _get_api_key()
    if not key:
        return {"ok": False, "error": "未配置 API Key"}
    # 先拿详情里的原始 inputNodes（含 fieldData/description 元数据）
    d = _market_detail(webapp_id)
    if not d["ok"]:
        return d
    raw_nodes = (d["detail"]["nodes"] or [])
    # 组装 nodeInfoList；ref 类型（本地素材/产物）先上传换云端文件名
    node_list = []
    for n in raw_nodes:
        val = node_values.get(n["k"])
        if val is None or val == "":
            continue
        if n["type"] == "ref":
            fp = _resolve_ref_path(val)
            if not fp:
                return {"ok": False, "error": f"参考文件不存在: {val}"}
            try:
                import rh_zimage_api as R
                _inject_api_key(R)  # 注入 key，否则上传鉴权失败
                up = R.upload_image(fp)
            except Exception as e:
                return {"ok": False, "error": f"上传参考文件失败: {e}"}
            val = up
        entry = {"nodeId": n.get("nodeId"), "fieldName": n["k"], "fieldValue": val}
        node_list.append(entry)
    body = {"webappId": webapp_id, "apiKey": key, "nodeInfoList": node_list}
    if webhook:
        body["webhookUrl"] = webhook
    if instance_type:
        body["instanceType"] = instance_type
    r = _rh_web("POST", "/task/openapi/ai-app/run", body, timeout=30)
    if not r["ok"]:
        return r
    d2 = r["data"] or {}
    return {"ok": True, "task_id": d2.get("taskId"), "task_status": d2.get("taskStatus"),
            "client_id": d2.get("clientId"), "prompt_tips": d2.get("promptTips")}


def _get_llm_cfg():
    cfg = _load_config()
    return {
        "base_url": (cfg.get("llm_base_url") or "").strip(),
        "model": (cfg.get("llm_model") or "").strip(),
        "api_key": (cfg.get("llm_api_key") or "").strip(),
        "style": (cfg.get("llm_style") or "natural").strip(),
    }


def _mask(s):
    return (s[:6] + "****" + s[-4:]) if len(s) >= 12 else (bool(s) and "****" or "")


# 按脚本类型选润色 system prompt（写法规范来自 gitee-prompt-library / luoxue-character-appearance / luoxue-video-series）
_POLISH_PROMPTS = {
    "t2i": (
        "你是一名 AI 绘画提示词专家，按固定结构润色用户描述为完整提示词。"
        "输出必须严格按以下段落顺序组织，每段一行："
        "①外貌锚点段：若用户描述里没有人物外貌，则使用这段固定开头——「棕色双麻花辫发型，两条麻花辫垂在胸前两侧，棕色头发，发尾微卷，齐刘海，中国女性，东方亚洲面孔，黄皮肤，二十多岁东亚女性，脸型小巧精致轮廓柔和流畅，五官秀气协调，眼睛大小自然，鼻型精致，嘴唇柔软，肌肤白皙细腻并保留真实皮肤质感，丰满巨乳，成熟少妇感温柔慵懒妩媚」；若用户已给外貌则保留其原意外貌。"
        "②场景段：先写大环境（时间+地点+氛围），再补充具体细节元素，由远及近。"
        "③光线段：具体化光源（侧逆光/霓虹反射/百叶窗斜射/闪光直打等），写明光线效果。"
        "④动作段：按递进顺序描述人物动作/姿态，层层展开，不用进行时。"
        "⑤服装段：按外层到内层到鞋袜的顺序从外向内描述，写明面料与贴合感。"
        "⑥镜头语言段：机位→构图→元素平衡，比如浅景深/电影感/全身入镜/竖构图。"
        "⑦氛围收尾段：用3-4个氛围词收尾（真实照片质感/私密松弛/冷艳/胶片感等）。"
        "整体保持用户原意不跑偏，不要让动作或场景喧宾夺主；直接输出润色后的提示词正文，不要任何解释、标头、代码块、序号。"
    ),
    "i2i": (
        "你是一名 AI 图生图提示词专家。原图已有角色、服装、姿势和场景，提示词的作用是驱动对原图的局部修改。"
        "输出要求："
        "①只描述需要修改的内容（风格、光影、表情、环境、服装细节等），不需要的部分不要写；"
        "②人物姿势与参考图保持一致，如需写姿势必须与原图同向，不写会带偏的新姿势；"
        "③不引入会改变角色身份的新元素（不换人、不换主要服装款式、不换性别年龄）；"
        "④外貌描述若被修改需求涉及才写，否则省略；"
        "⑤按「修改目标→修改细节→光线/氛围」的顺序组织，短句优先，克制长度，不要长篇大论。"
        "直接输出润色后的提示词正文，不要任何解释、标头、代码块、序号。"
    ),
    "video": (
        "你是一名 AI 视频生成提示词专家，按视频分镜规范润色用户的动作/运镜描述。"
        "输出必须严格按「分镜表」格式组织：每个时间段一行，用竖线分隔五个字段——`时间段｜画面｜动作｜台词｜音轨`。"
        "各字段要求："
        "①时间段：如 0-2s / 2-4s / 4-6s，严格按照时间先后组织全部内容，禁止无时间段标记的平铺描述；"
        "②画面：该时段的机位/镜头运动与取景内容（如『镜头从霓虹招牌缓缓推近』）；"
        "③动作：一律写已完成状态的结果动作（『裙摆被风掀起』而非『正在掀裙摆』），全体动作密度控制在5-8个，不要堆砌超过20个，每个动作讲清楚谁做的、作用于什么；"
        "④台词：人物所说的话，短句、带语气词的软糯气声口吻，25字以内；该时段无台词则写「无」；"
        "⑤音轨：该时段的背景音乐/拟声词/环境音（如『低频电子BGM渐入』『高跟鞋哒哒声』『雨声渐弱』），每个时段都要写明音轨内容，人物不说话也要有环境音；"
        "附加规则：同一时段内若有多人/多手，写清楚各自在做什么，不混同；首帧内容为基准，动作从首帧状态出发自然推进，结尾落在明确的状态上；若原描述含第一人称/镜面视角，保持该视角不混入第三人称全身镜头。"
        "只输出分镜表正文（每行一个时间段），不要任何解释、标头、代码块、序号。"
    ),
}
_DEFAULT_POLISH = _POLISH_PROMPTS["t2i"]


def _clean_polished(content):
    """裁剪 LLM 返回：剥代码块/引号/装饰前缀/客套尾巴/多余空白，只留提示词正文"""
    import re as _re
    s = (content or "").strip()
    # 剥 markdown 代码块围栏
    s = _re.sub(r"^```[a-zA-Z]*\s*", "", s).strip()
    s = _re.sub(r"\s*```$", "", s).strip()
    # 剥「【xxx】」类包前缀（只认中文方括号【】，避免误杀「」全引用）
    s = _re.sub(r"^【\s*[^】]{0,8}\s*】\s*", "", s).strip()
    # 若开头是引号族，整段被配对引号包裹 → 找闭合引号剥掉包皮（保留闭合后的杂话待后续处理）
    _pairs = {'"': '"', "'": "'", '「': '」', '『': '』', '“': '”', '‘': '’', '【': '】'}
    if s and s[0] in _pairs:
        close = _pairs[s[0]]
        idx = s.find(close, 1)
        if idx != -1 and idx * 2 >= len(s):   # 闭合引号在文本后半段才算"整段包裹"
            s = (s[1:idx] + s[idx+1:]).strip()
    # 剥残留的首尾引号/书名号
    s = _re.sub(r"^[\"'「『【“‘]+|[\"'」』】”’]+$", "", s).strip()
    # 剥常见标头前缀
    for pre in ("提示词：", "提示词:", "Prompt:", "prompt:", "润色结果：", "润色结果:", "以下是", "润色后：", "润色后:"):
        if s.startswith(pre):
            s = s[len(pre):].strip()
    # 折叠多余空行
    s = _re.sub(r"\n{2,}", "\n", s).strip()
    # 剥客套尾巴
    s = _re.sub(r"[（(]完[)）]\.?$", "", s).strip()
    s = _re.sub(r"(希望你喜欢|以上就是|就这样|希望能帮到你|如你所愿|建议直接使用)[！!。.]*$", "", s).strip()
    return s.strip()


def _backend_status():
    """后端运行状态：PID、端口、Python 版本、启动时间、配置路径。"""
    try:
        import datetime
        py_ver = sys.version.split()[0]
        started = datetime.datetime.fromtimestamp(START_TIME).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "ok": True, "pid": os.getpid(), "port": RUNNING_PORT,
            "python": py_ver, "started": started,
            "base": str(BASE), "tasks": len(TASKS),
            "mode": "android" if os.environ.get("ANDROID_ROOT") else "local",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _restart_backend():
    """重启后端：先优雅关掉当前 server（由调用方在子线程做），再用 os.execv 替换进程。"""
    # 标记需要重启，由 do_POST 返回前触发（避免重启后响应丢失）
    _RESTART_FLAG[0] = True
    return {"ok": True, "msg": "后端即将重启，页面会自动重连"}


# ============================================================
# 更新检测（v0.5.1）：GitHub 最新 Release vs 当前版本
# 免 API 方案：latest 重定向 → 解析 tag，绕开 GitHub API 限流(60次/h)
# ============================================================
CUR_VERSION = "0.5.5"          # 当前版本号（build.gradle versionName）
_UPDATE_CACHE = {"ts": 0, "data": None}   # 15 分钟缓存，避免频繁打外网

def _version_tuple(v):
    """'0.4.9.1' → (0,4,9,1)；'v0.5.0-alpha' 之类取数字段。解析失败返回 (0,)。"""
    import re as _re
    m = _re.findall(r"\d+", str(v))
    return tuple(int(x) for x in m[:4]) or (0,)

def _update_check(force=False):
    """查 GitHub 最新 Release。返回 {"ok":True,"update":bool,"current":...,"latest":...,"url":...,"notes":...}"""
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and now - _UPDATE_CACHE["ts"] < 900:
        return _UPDATE_CACHE["data"]
    try:
        import urllib.request
        import urllib.error

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            """禁跟随重定向，捕获 302 拿 Location 里的版本号"""
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect())
        req = urllib.request.Request("https://github.com/yuccc7878/runninghub-webui/releases/latest",
                                     method="HEAD", headers={"User-Agent": "runninghub-webui/"+CUR_VERSION})
        loc = ""
        try:
            opener.open(req, timeout=10)
        except urllib.error.HTTPError as e:
            # 302/301 → Location: /yuccc7878/runninghub-webui/releases/tag/v0.4.9.1
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location") or ""
        if not loc:
            return {"ok": False, "error": "获取最新版本失败(无重定向)", "current": CUR_VERSION}
        latest = (loc.rsplit("/", 1)[-1] or "").lstrip("v")   # 版本号段
        cur = CUR_VERSION.lstrip("v")
        is_new = _version_tuple(latest) > _version_tuple(cur)
        data = {"ok": True, "update": is_new, "current": CUR_VERSION,
                "latest": latest,
                "url": "https://github.com/yuccc7878/runninghub-webui/releases/latest",
                "notes": ""}
        _UPDATE_CACHE["ts"], _UPDATE_CACHE["data"] = now, data
        return data
    except Exception as e:
        return {"ok": False, "error": str(e), "current": CUR_VERSION}


START_TIME = time.time()
_RESTART_FLAG = [False]
# 实际监听端口（本地 --port / app 8765），_backend_status 用它返回真实值
RUNNING_PORT = PORT


def _polish_prompt(prompt_text, kind, llm):
    """调 OpenAI 兼容接口润色提示词，返回裁剪后的正文"""
    import urllib.request
    base = llm["base_url"].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = base + "/v1/chat/completions"
    style_note = {
        "natural": "用自然语言描述，一段话，直接可读。",
        "tags": "用英文逗号分隔的关键词 tags 形式输出，如 '1girl, long brown hair, ...'。",
        "brief": "简练关键词，去掉修饰性虚词，只留画面元素与属性。",
    }.get(llm.get("style") or "natural", "")
    if llm.get("style") == "tags":
        lang_note = "全部使用英文 tags。"
    else:
        lang_note = "语言与用户输入保持一致（用户中文就输出中文，英文就英文）。"
    sysp = (_POLISH_PROMPTS.get(kind) or _DEFAULT_POLISH) + " " + style_note + " " + lang_note
    body = json.dumps({
        "model": llm["model"],
        "messages": [
            {"role": "system", "content": sysp},
            {"role": "user", "content": prompt_text[:3000]},
        ],
        "temperature": 0.8,
        "max_tokens": 256000,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {llm['api_key']}")
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    msg = resp["choices"][0]["message"]
    # 部分带 reasoning 的模型会把 token 吃在思考上，content 可能为空 → 用 reasoning_content 兜底
    content = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
    s = _clean_polished(content)
    if not s:
        raise RuntimeError("LLM 返回为空")
    return s


def _inject_api_key(mod):
    """把已配置的 RunningHub API Key 注入底层脚本模块（一次性注入所有模块，防漏）"""
    key = _get_api_key()
    if not key:
        return False
    mods = [mod]
    # 无论传哪个模块，都把所有底层脚本模块一起注入，避免跨模块调用漏注入
    for mname in ("rh_zimage_app", "rh_zimage_api", "rh_krea2_app", "rh_krea2_nsfw", "rh_h3_flf_api", "rh_workflow_api", "rh_wan22", "rh_reactor"):
        try:
            m = importlib.import_module(mname)
            if m not in mods:
                mods.append(m)
        except Exception:
            pass
    for m in mods:
        try:
            if hasattr(m, "API_KEY"):
                m.API_KEY = key
            if hasattr(m, "API_KEY_BACKUP"):
                m.API_KEY_BACKUP = key
            if hasattr(m, "PROXY"):
                m.PROXY = f"{m.BASE}/proxy/{key}" if hasattr(m, "BASE") else f"https://www.runninghub.ai/proxy/{key}"
        except Exception:
            pass
    return True


def _load_state():
    """启动时从 webui_state.json 恢复历史任务（跨重启保留）。

    必须在 BASE 指向运行时目录后再调用：安卓下 serve.py 会覆盖 rh_webui.BASE，
    若在模块级就调用，会读到 AssetFinder/workflows（不存在）导致历史丢失。
    """
    try:
        f = BASE / "workflows" / "webui_state.json"
        if not f.exists():
            return
        data = json.loads(f.read_text(encoding="utf-8"))
        for tid, info in data.items():
            TASKS[tid] = info
            m = re.match(r"^T(\d+)_", tid)
            if m:
                n = int(m.group(1))
                if n > TASK_SEQ[0]:
                    TASK_SEQ[0] = n
    except Exception:
        pass


def _recover_stale_tasks():
    """启动后自动复核上次进程遗留的『假活』任务（queued/submitting/running/poll_timeout）。

    进程被杀/重启 → 轮询线程消失 → 任务状态永远卡住 → 前端显示提交中、提交锁被占死。
    对每个遗留活跃任务 spawn 后台线程查远端：已完成→拉回落盘；还在跑→续轮询；
    远端已失败 或 远端不存在 → 标记 error + 置 resubmit_pending，
        由前端弹窗让用户决定是否重新提交（绝不自动重提，避免重复烧币）；
    查询失败（网络）→ 保持原状态，留给用户手动错误查询。"""
    with _lock:
        stale = [(tid, t.get("pid"), t.get("script") or "") for tid, t in TASKS.items()
                 if t.get("status") in ("queued", "submitting", "running", "poll_timeout")]
    if not stale:
        return
    print(f"[recover] 发现 {len(stale)} 个遗留活跃任务，后台复核中")

    def _one(tid, pid, script_key):
        try:
            if script_key not in REGISTRY:
                return
            mod = _load_module(REGISTRY[script_key]["module"])
            # 必须先注入 Key：进程刚启动时模块内 API_KEY 还是空串，
            # 直接查远端会返回 301 must not be blank，导致遗留任务全部卡死
            _inject_api_key(mod)
            if not pid:
                # 无 pid：无法续，但绝不重提，标记为中断
                with _lock:
                    TASKS[tid]["status"] = "error"
                    TASKS[tid]["error"] = "进程重启导致任务中断（无任务号，无法恢复）"
                _save_state()
                return
            st = _check_pid_status(pid, mod)
            if st["status"] == "success":
                try:
                    saved = _download_from_entry(tid, mod, st["detail"], pid)
                    print(f"[recover] {tid} 远端已完成，拉回 {len(saved)} 个产物")
                except Exception as e:
                    with _lock:
                        TASKS[tid]["status"] = "error"
                        TASKS[tid]["error"] = f"拉回失败: {e}"
                    _save_state()
                return
            if st["status"] in ("pending", "running", "queued"):
                _resume_poll(tid, mod, pid)
                return
            # 远端已失败：标记 error 并挂起「是否重提」的询问，由前端弹窗决定
            if st["status"] == "error":
                with _lock:
                    TASKS[tid].update({
                        "status": "error",
                        "error": f"远端执行失败: {st['detail']}",
                        "resubmit_pending": True,
                    })
                _save_state()
                print(f"[recover] {tid} 远端已失败，等待用户确认是否重提")
                return
            if st["status"] == "not_found":
                with _lock:
                    TASKS[tid].update({
                        "status": "error",
                        "error": "远端任务不存在（可能已过期），已标记失败",
                        "resubmit_pending": True,
                    })
                _save_state()
                print(f"[recover] {tid} 远端任务不存在，等待用户确认是否重提")
                return
            # api_error：网络问题，保持原状态，留给用户手动错误查询
            print(f"[recover] {tid} 远端查询失败，保持原状态")
        except Exception as e:
            print(f"[recover] {tid} 复核异常: {e}")

    for tid, pid, script_key in stale:
        threading.Thread(target=_one, args=(tid, pid, script_key), daemon=True).start()


REGISTRY = {
    "zimage_i2i": {
        "name": "Zimage 图生图/文生图 (I2I+T2I · App)",
        "desc": "走已发布 App 通道。选图=图生图（换装/换风格，保留人物身份），不选图=文生图。主力方案",
        "module": "rh_zimage_app", "mode": "i2i",
        "params": [
            {"k": "ref_image", "label": "参考图", "type": "ref", "accepts": "image", "def": "", "help": "不选 = 文生图（走 zimage文生图 App）"},
            {"k": "resolution", "label": "分辨率", "type": "select", "options": [
                {"v": "768x1368", "label": "768×1368（竖屏 9:16 · 省币）"},
                {"v": "1080x1920", "label": "1080×1920（竖屏 9:16 · 推荐）"},
                {"v": "1242x2688", "label": "1242×2688（竖屏 9:16 · 高清）"},
                {"v": "1024x1024", "label": "1024×1024（方形 1:1）"},
                {"v": "1368x768", "label": "1368×768（横屏 16:9 · 省币）"},
                {"v": "1920x1080", "label": "1920×1080（横屏 16:9 · 高清）"},
            ], "def": "1080x1920"},
            {"k": "steps", "label": "步数", "type": "int", "def": 6, "help": "turbo 蒸馏模型，6 步足够；8-10 细节略多但更慢"},
            {"k": "denoise", "label": "重绘强度", "type": "float", "def": 0.65, "help": "仅图生图生效。0.4-0.5 轻度改 / 0.65 换装保留人物 / 0.85+ 结构漂移"},
            {"k": "lora_a", "label": "LoRA 位A · 瑶光位", "type": "lora_name", "def": False, "def_strength": 0.3, "placeholder": "留空=沿用 Kook_Zimage_瑶光；可填真实文件名或短名", "help": "仅图生图生效。接在 UNET 之后，运行时替换任意 LoRA。短名可填 charm / nofilter / flowdpo / model / peach / zanime"},
            {"k": "lora_b", "label": "LoRA 位B · 如梦位", "type": "lora_name", "def": False, "def_strength": 0.25, "placeholder": "留空=沿用 Kook_Zimage_如梦似幻；可填真实文件名或短名", "help": "仅图生图生效。接在位A 之后"},
            {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机（App 未暴露，仅记录）", "group": "advanced"},
        ],
    },
    "krea2": {
        "name": "Krea2 图生图/文生图 (I2I+T2I · App)",
        "desc": "走已发布 App 通道。选图=图生图（换装/换风格，保留人物身份），不选图=文生图。三个 LoRA 位可在运行时换名",
        "module": "rh_krea2_app", "mode": "i2i",
        "params": [
            {"k": "ref_image", "label": "参考图", "type": "ref", "accepts": "image", "def": "", "help": "不选 = 文生图（走 krea2快速省币 App）；选了 = 图生图（走 krea2图生图极速省币 App）。参考图只注入 latent 起点，不进入 CLIP"},
            {"k": "resolution", "label": "分辨率", "type": "select", "options": [
                {"v": "832x1216", "label": "832×1216（竖屏 2:3 · 省币）"},
                {"v": "1080x1920", "label": "1080×1920（竖屏 9:16 · 推荐）"},
                {"v": "1216x832", "label": "1216×832（横屏 3:2）"},
                {"v": "1024x1024", "label": "1024×1024（方形 1:1）"},
                {"v": "1920x1080", "label": "1920×1080（横屏 16:9）"},
            ], "def": "1080x1920", "help": "写入 39/40 两个 Int 节点；图生图时同时决定 43 ImageScale 的输出尺寸"},
            {"k": "steps", "label": "步数", "type": "int", "def": 8, "help": "Krea2 蒸馏模型 6-8 步足够"},
            {"k": "denoise", "label": "重绘强度", "type": "float", "def": 0.65, "help": "仅图生图生效。0.4-0.5 微调 / 0.65 换装保人物 / 0.75+ 结构开始漂移 / 1.0 等于重画（画面必崩）"},
            {"k": "style", "label": "LoRA 画风位 · 风情万种", "type": "lora_name", "def": True, "def_strength": 0.8, "placeholder": "留空 = 沿用 krea2-Cc风情万种-风格", "help": "节点 28。可填短名 charm 或任意真实文件名。留空沿用发布默认：文生图 1.0 / 图生图 0.8"},
            {"k": "realism", "label": "LoRA 写实位 · 真实感引擎", "type": "lora_name", "def": True, "def_strength": 0.6, "placeholder": "留空 = 沿用 realism_engine_krea2_v3.1_new", "help": "节点 29。可填短名 real。留空沿用发布默认：文生图 0.5 / 图生图 0.6"},
            {"k": "body", "label": "LoRA 身材位 · 天魔", "type": "lora_name", "def": True, "def_strength": 1.0, "placeholder": "留空 = 沿用 krea2-Cc-天魔-身材", "help": "节点 30。可填短名 body。留空沿用发布默认：文生图 0.85 / 图生图 1.0"},
            {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机（App 未暴露，仅记录）", "group": "advanced"},
        ],
    },
    "h3_flf_video": {
        "name": "MiniMaxH3 首尾帧视频 (I2V)",
        "desc": "首帧图(→尾帧图) → 视频。支持单段/续接，输出 mp4",
        "module": "rh_h3_flf_api", "mode": "video",
        "params": [
            {"k": "first_ref", "label": "首帧图", "type": "ref", "accepts": "image", "def": ""},
            {"k": "last_ref", "label": "尾帧图(可选)", "type": "ref", "accepts": "image", "def": ""},
            {"k": "ratio", "label": "比例", "type": "select", "options": ["9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "1:1 (Square)", "3:4 (Portrait Standard)", "4:3 (Standard)", "21:9 (Ultrawide)"], "def": "9:16 (Portrait Widescreen)"},
            {"k": "megapixels", "label": "分辨率", "type": "select", "options": [{"v": "0.25", "label": "0.25 MP（低清快出）"}, {"v": "0.4", "label": "0.4 MP（标清）"}, {"v": "0.6", "label": "0.6 MP（推荐）"}, {"v": "0.9", "label": "0.9 MP（高清）"}, {"v": "1.2", "label": "1.2 MP（超清）"}], "def": "0.6"},
            {"k": "duration", "label": "时长(秒)", "type": "float", "def": 5, "help": "视频长度，越长越贵"},
            {"k": "steps", "label": "步数", "type": "int", "def": 8},
            {"k": "lora", "label": "LoRA", "type": "select", "options": [{"v": "none", "label": "无"}, {"v": "synthpussy", "label": "synthpussy（阴部细节加强，实测✓）"}, {"v": "realface", "label": "realface（人物真实质感，触发词 r34l1sm）"}, {"v": "cinematic", "label": "cinematic（电影级质感，触发词 DY）"}, {"v": "physics", "label": "physics（物理运动，不稳定）"}, {"v": "nokia", "label": "nokia（诺基亚复古低清，触发词 nokiastyle）"}], "def": "none"},
            {"k": "lora_strength", "label": "LoRA强度", "type": "float", "def": 0.9, "help": "仅 LoRA≠none 时生效", "group": "advanced"},
            {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机", "group": "advanced"},
        ],
    },
    "h3_omni_video": {
        "name": "MiniMaxH3 全能视频 (T2V/参考)",
        "desc": "全能力：文生视频 / 参考图/视频/音频 omni，输出 mp4",
        "module": "rh_workflow_api", "mode": "video",
        "params": [
            {"k": "ref_image", "label": "参考图(可选)", "type": "ref", "accepts": "image", "def": ""},
            {"k": "mode", "label": "模式", "type": "select", "options": [{"v": "", "label": "自动(有参考图→omni，无→文生视频)"}, {"v": "text_to_video", "label": "文生视频 T2V"}, {"v": "omni_reference", "label": "参考图驱动 omni"}], "def": ""},
            {"k": "ratio", "label": "比例", "type": "select", "options": ["9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "1:1 (Square)", "3:4 (Portrait Standard)", "4:3 (Standard)", "21:9 (Ultrawide)"], "def": "9:16 (Portrait Widescreen)"},
            {"k": "megapixels", "label": "分辨率", "type": "select", "options": [{"v": "0.25", "label": "0.25 MP（低清快出）"}, {"v": "0.4", "label": "0.4 MP（标清）"}, {"v": "0.6", "label": "0.6 MP（推荐）"}, {"v": "0.9", "label": "0.9 MP（高清）"}, {"v": "1.2", "label": "1.2 MP（超清）"}], "def": "0.4"},
            {"k": "duration", "label": "时长(秒)", "type": "float", "def": 5, "help": "视频长度，越长越贵"},
            {"k": "steps", "label": "步数", "type": "int", "def": 8},
            {"k": "lora", "label": "LoRA", "type": "select", "options": [{"v": "none", "label": "无"}, {"v": "synthpussy", "label": "synthpussy（阴部细节加强，实测✓）"}, {"v": "realface", "label": "realface（人物真实质感，触发词 r34l1sm）"}, {"v": "cinematic", "label": "cinematic（电影级质感，触发词 DY）"}, {"v": "physics", "label": "physics（物理运动，不稳定）"}, {"v": "nokia", "label": "nokia（诺基亚复古低清，触发词 nokiastyle）"}], "def": "none"},
            {"k": "lora_strength", "label": "LoRA强度", "type": "float", "def": 0.9, "help": "仅 LoRA≠none 时生效", "group": "advanced"},
            {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机", "group": "advanced"},
        ],
    },
     "reactor_swap": {
        "name": "ReActor 只换脸（视频）",
        "desc": "像素级换脸：只替换源视频里的人脸，身体/动作/服装/背景原样保留。输出自动贴合源视频分辨率。⚠️ 脸型差异过大会变形；要「连人带货一起换」请用 Wan Animate",
        "module": "rh_reactor", "mode": "video",
        "params": [
            {"k": "source_face", "label": "参考脸（要换上去的脸）", "type": "ref", "accepts": "image", "def": "", "help": "提供人脸的图，建议正面清晰照"},
            {"k": "src_video", "label": "源视频（被换脸）", "type": "ref", "accepts": "video", "def": "", "help": "身体/动作/服装/背景来自它，输出分辨率自动跟随源视频"},
            {"k": "max_side", "label": "最长边上限", "type": "select", "options": [
                {"v": "512", "label": "512（最快，低清）"},
                {"v": "768", "label": "768（平衡）"},
                {"v": "1024", "label": "1024（默认 · 推荐）"},
                {"v": "1280", "label": "1280（超清 · 慢）"},
            ], "def": "1024", "help": "源视频等比缩放到此上限（保持原始宽高比），防止爆显存"},
            {"k": "compare", "label": "额外输出对比版（右拼源视频）", "type": "checkbox", "def": False},
            {"k": "face_restore", "label": "脸部修复强度", "type": "float", "def": 0.4, "help": "GFPGAN 修复，0=不修复（更原始）/ 0.4=默认 / 1=磨皮最重", "group": "advanced"},
            {"k": "interp", "label": "RIFE 补帧倍率", "type": "select", "options": [
                {"v": "2", "label": "2×（默认 · 推荐）"},
                {"v": "4", "label": "4×（最顺滑，最慢）"},
                {"v": "1", "label": "1×（不补帧 · 仅调试用）"},
            ], "def": "2", "help": "⚠️ 本工作流是「关键帧换脸 + RIFE 补帧」：只挑帧换脸，其余靠补帧过渡。关闭补帧（1×）画面会拖慢/顿挫，默认保持 2× 即可"},
        ],
    },
    "wan22_animate": {
         "name": "动作迁移 · Wan Animate 2.2",
         "desc": "生成式角色替换：参考图定身份（脸/发型/服装），源视频定动作。⚠️ 会连同服装/背景一起重画；要「只换脸」请用 ReActor",
         "module": "rh_wan22", "mode": "video",
         "params": [
             {"k": "ref_image", "label": "参考图（身份）", "type": "ref", "accepts": "image", "def": "", "help": "决定生成人物的长相/形象"},
             {"k": "ref_video", "label": "源视频（动作）", "type": "ref", "accepts": "video", "def": "", "help": "提供动作/姿态（只列视频素材）"},
             {"k": "resolution", "label": "分辨率（宽×高）", "type": "select", "options": [
                {"v": "480x832", "label": "480×832（竖屏 · 推荐，实测通过）"},
                {"v": "544x960", "label": "544×960（竖屏 · 实测通过，稍慢）"},
                {"v": "576x1024", "label": "576×1024（竖屏 · 高清）"},
                {"v": "640x640", "label": "640×640（方形）"},
                {"v": "768x768", "label": "768×768（方形 · 高清）"},
                {"v": "832x480", "label": "832×480（横屏）"},
                {"v": "960x544", "label": "960×544（横屏 · 高清）"},
                {"v": "1024x576", "label": "1024×576（横屏 · 超清）"},
            ], "def": "480x832", "help": "实测：720×1280 会显存溢出(OOM)失败，更高的竖屏档位请自行试跑"},
             {"k": "face_enhance", "label": "人脸增强", "type": "checkbox", "def": True, "help": "启用真实人脸检测（关闭则走黑图，相似度下降）"},
             {"k": "compare", "label": "额外输出对比条（含流程拼接）", "type": "checkbox", "def": False},
             {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机", "group": "advanced"},
             {"k": "steps", "label": "步数", "type": "int", "def": 6, "help": "默认 6（工作流原值）", "group": "advanced"},
         ],
     },
     "illustrious_t2i": {
        "name": "Illustrious 二次元文生图 (T2I)",
        "desc": "WAI-illustrious-SDXL 二次元底模（自带动漫画风）。可选加 Smooth_Booster 画质增强 / Cunnystyle 插画微调。半写实 CG 动漫风",
        "module": "rh_illustrious_api", "mode": "t2i",
        "params": [
            {"k": "resolution", "label": "分辨率", "type": "select", "options": [
                {"v": "936x1368", "label": "936×1368（竖屏 9:16 · 推荐）"},
                {"v": "768x1344", "label": "768×1344（竖屏 9:16 · 低清）"},
                {"v": "1080x1920", "label": "1080×1920（竖屏 9:16 · 高清，更贵）"},
                {"v": "1216x832", "label": "1216×832（横屏 16:9）"},
                {"v": "1024x1024", "label": "1024×1024（方形 1:1）"},
            ], "def": "936x1368"},
            {"k": "steps", "label": "步数", "type": "int", "def": 20},
            {"k": "cfg", "label": "CFG", "type": "float", "def": 2.0, "help": "Illustrious 系建议 2.0 左右"},
            {"k": "smooth", "label": "画质增强 Smooth_Booster", "type": "lora", "def": True, "def_strength": 0.9, "help": "画质平滑/细节增强（通用，二次元底模也适用）"},
            {"k": "cunnystyle", "label": "插画微调 Cunnystyle", "type": "lora", "def": False, "def_strength": 0.8, "help": "平台工作流自带插画风格微调（底模已是二次元，这层可选加味，具体效果待实测）"},
            {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机", "group": "advanced"},
        ],
    },
}


def _load_module(name):
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)


def _list_refs():
    if not REFS_DIR.exists():
        return []
    return sorted([p.name for p in REFS_DIR.iterdir()
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov")])


def _list_outputs():
    """webui_out 里的出图产物（图/视频，可当参考图/首帧用）"""
    if not OUT_DIR.exists():
        return []
    return sorted([p.name for p in OUT_DIR.iterdir()
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".mp4")])


def _resolve_ref_path(ref):
    """解析参考图路径：'out:文件名' → 输出目录；否则 → refs 目录。不存在返回 None"""
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("out:"):
        name = os.path.basename(ref[4:])
        fp = OUT_DIR / name
        return str(fp) if fp.exists() else None
    name = os.path.basename(ref)
    fp = REFS_DIR / name
    return str(fp) if fp.exists() else None


def _save_state():
    """落盘历史任务。带防误删保护：
    若内存里 TASKS 为空、而磁盘上已有非空记录，说明当前是「另一个进程」
    （例如脚本 import rh_webui 后调函数），此刻写盘会把真正的历史清空 —— 直接跳过。"""
    try:
        f = BASE / "workflows" / "webui_state.json"
        if not TASKS and f.exists():
            try:
                old = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                old = {}
            if old:
                print(f"[state] 拒绝写空：内存无任务但磁盘有 {len(old)} 条，"
                      f"疑似独立进程调用，已跳过（避免清空历史）", flush=True)
                return
        Path(BASE / "workflows").mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换，避免写一半断电/被杀导致文件损坏
        tmp = f.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump({k: {kk: vv for kk, vv in v.items() if kk != "trace"}
                       for k, v in TASKS.items()}, fh, ensure_ascii=False)
        os.replace(str(tmp), str(f))
    except Exception:
        pass


# ===== API 出图 → 历史记录 =====
API_SCRIPT_KEY = "api"


def _next_task_id():
    """分配历史任务号（与网页提交共用 TASK_SEQ，保证 T 编号不重复）。"""
    TASK_SEQ[0] += 1
    return f"T{TASK_SEQ[0]:03d}_{int(time.time())}"


def _register_api_task(params=None, files=None, image=None, pid=None):
    """把 OpenAI 兼容层（/v1/images/generations）的产物登记为一条历史任务。
    这样 API 出的图和网页出的图在历史里统一可见、可预览、可删除。
    产物已由调用方写入 OUT_DIR（webui_out），这里只建记录 + 落盘 state。"""
    if not image:
        return None
    with _lock:
        tid = _next_task_id()
        TASKS[tid] = {
            "script": API_SCRIPT_KEY,
            "params": params or {},
            "status": "done",
            "pid": str(pid) if pid else None,
            "image": image,
            "files": list(files or [image]),
            "cost": {},
            "error": None,
            "ts": time.time(),
            "source": "api",
        }
    _save_state()
    return tid


def _export_to_public(files):
    """把产物导出到公共目录（Pictures|Movies|Download/RunningHub）。
    仅安卓环境生效（通过 Chaquopy jclass 调 Kotlin Exporter），本地静默跳过。
    files: 文件名列表（相对 OUT_DIR）。返回成功导出的文件名列表。"""
    if not files:
        return []
    try:
        from java import jclass
        Exporter = jclass("com.yuccc.runninghub.Exporter")
        paths = []
        for f in files:
            p = OUT_DIR / os.path.basename(str(f))
            if p.exists():
                paths.append(str(p))
        if not paths:
            return []
        done = list(Exporter.exportList(paths) or [])
        return done
    except Exception:
        return []


def _extract_last_frame(video_path, out_path):
    """抽取视频尾帧存为 out_path（jpg）。环境无关，成功返回 True。
      1) 本地/Ubuntu → ffmpeg（-sseof 取末尾一帧）
      2) Android/Chaquopy → Kotlin VideoFrames（MediaMetadataRetriever）
    """
    video_path = str(video_path)
    out_path = str(out_path)
    if not os.path.exists(video_path):
        return False
    # 1) ffmpeg（本地/Ubuntu）
    try:
        import subprocess
        r = subprocess.run(
            ["ffmpeg", "-y", "-sseof", "-0.5", "-i", video_path,
             "-vframes", "1", "-q:v", "2", out_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
    except Exception:
        pass
    # 2) Android Kotlin（MediaMetadataRetriever）
    try:
        from java import jclass
        VF = jclass("com.yuccc.runninghub.VideoFrames")
        if (VF.extractLastFrame(video_path, out_path)
                and os.path.exists(out_path) and os.path.getsize(out_path) > 0):
            return True
    except Exception:
        pass
    return False


def _task_lastframe(tid):
    """抽取某任务的视频尾帧，存入 REFS_DIR。返回 (ok, result_dict)。"""
    with _lock:
        t = TASKS.get(tid)
    if not t:
        return False, {"error": f"任务不存在: {tid}"}
    # 收集该任务的视频产物（优先 image，其次 files）
    cand = []
    if t.get("image"):
        cand.append(t["image"])
    cand += list(t.get("files") or [])
    vid = None
    for f in cand:
        if str(f).lower().endswith((".mp4", ".webm", ".mov", ".mkv", ".avi")):
            vid = str(f)
            break
    if not vid:
        return False, {"error": "该任务没有视频产物"}
    src = OUT_DIR / os.path.basename(vid)
    if not src.exists():
        return False, {"error": f"视频文件不存在: {os.path.basename(vid)}"}
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    out_name = "尾帧_" + Path(os.path.basename(vid)).stem + ".jpg"
    out_path = REFS_DIR / out_name
    if not _extract_last_frame(str(src), str(out_path)):
        return False, {"error": "抽帧失败（ffmpeg / MediaMetadataRetriever 均不可用）"}
    return True, {"ok": True, "name": out_name, "size": os.path.getsize(out_path)}


def _run_task(task_id, script_key, params):
    cfg = REGISTRY[script_key]
    try:
        mod = _load_module(cfg["module"])
        # 提交成功立即持久化 pid（on_submit 回调：pid 永不丢，app 被杀也能续）
        def _on_submit(pid):
            with _lock:
                TASKS[task_id]["pid"] = pid
                TASKS[task_id]["status"] = "submitting"
            _save_state()
        # 注入用户配置的 API Key（替代脚本内写死的 key）
        _inject_api_key(mod)
        if script_key == "wan22_animate":
            try:
                import rh_wan22 as _W
                _inject_api_key(_W)
            except Exception:
                pass
        if script_key == "reactor_swap":
            try:
                import rh_reactor as _RA
                _inject_api_key(_RA)
            except Exception:
                pass
        if script_key == "reactor_swap":
            # ReActor 只换脸：参考脸（要换上去）+ 源视频（被换脸）→ 像素级换脸
            face_ref = params.get("source_face") or ""
            video_ref = params.get("src_video") or ""
            face_path = _resolve_ref_path(face_ref)
            video_path = _resolve_ref_path(video_ref)
            if not face_path:
                raise RuntimeError("需要参考脸图片（素材库选一张正面清晰照）")
            if not video_path:
                raise RuntimeError("需要源视频（从素材库或出图产物选一段）")
            resolution = str(params.get("resolution") or "").strip()
            max_side = 1024
            try:
                max_side = int(params.get("max_side") or 1024)
            except (TypeError, ValueError):
                max_side = 1024
            compare = str(params.get("compare", "")).lower() in ("true", "1", "on")
            try:
                interp = int(params.get("interp", 2))
            except (TypeError, ValueError):
                interp = 2
            try:
                face_restore = float(params.get("face_restore", 0.4))
            except (TypeError, ValueError):
                face_restore = 0.4
            with _lock:
                TASKS[task_id]["status"] = "submitting"
            res = mod.run(
                source_face=face_path,
                src_video=video_path,
                max_side=max_side,
                interp=interp,
                compare=compare,
                face_restore=face_restore,
                download_dir=str(OUT_DIR),
                on_submit=_on_submit,
            )
            saved = res.get("saved", []) or []
            # 产物可能有多个（compare 时），image 优先取 mp4
            _img = next((f for f in saved if str(f).lower().endswith((".mp4", ".webm"))), saved[0] if saved else None)
            with _lock:
                TASKS[task_id].update({"status": "done", "image": _img,
                                       "files": saved, "pid": res.get("prompt_id"), "error": None})
            _save_state()
            return
        if script_key == "wan22_animate":
            # Wan Animate 2.2 动作迁移：参考图（身份）+ 源视频（动作）→ 生成式角色替换
            ref_ref = params.get("ref_image") or ""
            video_ref = params.get("ref_video") or ""
            ref_path = _resolve_ref_path(ref_ref)
            video_path = _resolve_ref_path(video_ref)
            if not video_path:
                raise RuntimeError("动作迁移需要源视频（从出图产物选一段）")
            if not ref_path:
                raise RuntimeError("动作迁移需要参考图（素材库选一张）")
            # 分辨率 "宽x高"
            w, h = 480, 832
            res_s = str(params.get("resolution") or "").strip().lower()
            if "x" in res_s:
                try:
                    w, h = (int(x) for x in res_s.split("x")[:2])
                except ValueError:
                    pass
            compare = str(params.get("compare", "")).lower() in ("true", "1", "on")
            face_enhance = str(params.get("face_enhance", "true")).lower() in ("true", "1", "on")
            seed_s = str(params.get("seed") or "").strip()
            try:
                seed = int(seed_s) if seed_s else None
            except ValueError:
                seed = None
            try:
                steps = int(params.get("steps", 6))
            except (TypeError, ValueError):
                steps = 6
            with _lock:
                TASKS[task_id]["status"] = "submitting"
            res = mod.run(
                ref_image_path=ref_path,
                ref_video_path=video_path,
                width=w, height=h,
                prompt_text=(params.get("prompt") or "").strip() or None,
                face_enhance=face_enhance,
                compare=compare,
                seed=seed, steps=steps,
                download_dir=str(OUT_DIR),
                on_submit=_on_submit,
            )
            videos = res.get("videos", []) or []
            with _lock:
                TASKS[task_id].update({"status": "done", "image": videos[0] if videos else None,
                                       "files": videos, "pid": res.get("prompt_id"), "error": None})
            _save_state()
            return

        prompt = params.get("prompt", "")

        if script_key in ("h3_flf_video", "h3_omni_video"):
            seed_s = str(params.get("seed") or "").strip()
            try:
                seed = int(seed_s) if seed_s else None
            except ValueError:
                seed = None
            lora = params.get("lora") or None
            if lora == "none":
                lora = None
            lora_st = float(params.get("lora_strength", 0.9)) if lora else None
            with _lock:
                TASKS[task_id]["status"] = "submitting"
            if script_key == "h3_flf_video":
                first_ref = params.get("first_ref") or ""
                last_ref = params.get("last_ref") or ""
                first_path = _resolve_ref_path(first_ref)
                last_path = _resolve_ref_path(last_ref)
                res = mod.run(
                    prompt_text=prompt,
                    first_frame_path=first_path,
                    last_frame_path=last_path,
                    aspect_ratio=params.get("ratio", "9:16 (Portrait Widescreen)"),
                    megapixels=float(params.get("megapixels", 0.6)),
                    duration=float(params.get("duration", 5)),
                    steps=int(params.get("steps", 8)),
                    noise_seed=seed,
                    download_dir=str(OUT_DIR),
                    lora=lora, lora_strength=lora_st,
                    on_submit=_on_submit,
                )
                videos = res.get("videos", []) or []
                with _lock:
                    TASKS[task_id].update({"status": "done", "image": videos[0] if videos else None,
                                           "files": videos, "pid": res.get("prompt_id"), "error": None})
            else:
                ref_image = params.get("ref_image") or ""
                ref_paths = None
                if ref_image:
                    rp = _resolve_ref_path(ref_image)
                    if rp: ref_paths = [rp]
                mode = params.get("mode") or None
                res = mod.run(
                    prompt_text=prompt,
                    ref_image_paths=ref_paths,
                    aspect_ratio=params.get("ratio", "9:16 (Portrait Widescreen)"),
                    megapixels=float(params.get("megapixels", 0.4)),
                    duration=float(params.get("duration", 5)),
                    steps=int(params.get("steps", 8)),
                    noise_seed=seed,
                    download_dir=str(OUT_DIR),
                    mode=mode,
                    lora=lora, lora_strength=lora_st,
                    on_submit=_on_submit,
                )
                saved = res.get("saved", []) or []
                # omni 工作流输出可能混入中间节点 png，image 优先选 mp4
                _img = next((f for f in saved if str(f).lower().endswith((".mp4", ".webm", ".mov"))), saved[0] if saved else None)
                with _lock:
                    TASKS[task_id].update({"status": "done", "image": _img,
                                           "files": saved, "pid": res.get("prompt_id"), "error": None})
            _save_state()
            return

        if script_key == "krea2":
            # 单入口双模：有参考图 → APP_I2I，无 → APP_T2I（mod.run 内部自动分流）
            res_s = str(params.get("resolution") or "").strip().lower()
            _w = _h = None
            if "x" in res_s:
                try:
                    _w, _h = (int(x) for x in res_s.split("x")[:2])
                except ValueError:
                    _w = _h = None
            width = _w or 1080
            height = _h or 1920
            ref = params.get("ref_image") or ""
            ref_path = None
            if ref:
                ref_path = _resolve_ref_path(ref)
                if not ref_path:
                    raise RuntimeError(f"参考图不存在: {ref}")
            # LoRA 三位：勾选且填了文件名才提交，否则沿用 App 发布时的默认 LoRA
            lora_kw = {}
            for slot, key, st_key in (("lora_style", "style", "style_strength"),
                                      ("lora_real", "realism", "real_strength"),
                                      ("lora_body", "body", "body_strength")):
                nm = str(params.get(key + "_name") or "").strip()
                if nm:
                    lora_kw[slot] = nm
                    lora_kw[st_key] = float(params.get(key + "_strength", 0.8))
            with _lock:
                TASKS[task_id]["status"] = "submitting"
            res = mod.run(
                prompt_text=prompt,
                ref_image_path=ref_path,
                width=width,
                height=height,
                steps=int(params.get("steps", 8)),
                denoise=float(params.get("denoise", 0.65)) if ref_path else None,
                download_dir=str(OUT_DIR),
                on_submit=_on_submit,
                verbose=False,
                **lora_kw,
            )
            saved = [os.path.basename(p) for p in (res.get("saved") or [])]
            with _lock:
                TASKS[task_id].update({
                    "status": "done", "image": saved[0] if saved else None,
                    "files": saved, "pid": res.get("task_id"), "error": None,
                })
            _save_state()
            return

        if script_key == "zimage_i2i":
            ref = params.get("ref_image") or ""
            # 有参考图 → 图生图 App；无 → 文生图 App
            ref_path = _resolve_ref_path(ref)
            seed_s = str(params.get("seed") or "").strip()
            try:
                seed = int(seed_s) if seed_s else None
            except ValueError:
                seed = None
            # 分辨率：下拉 "宽x高"，回退旧的 width/height 字段（历史任务重跑兼容）
            _w = _h = None
            res_s = str(params.get("resolution") or "").strip().lower()
            if res_s and "x" in res_s:
                try:
                    _w, _h = (int(x) for x in res_s.split("x")[:2])
                except ValueError:
                    _w = _h = None
            width = _w or int(params.get("width", 1080))
            height = _h or int(params.get("height", 1920))
            with _lock:
                TASKS[task_id]["status"] = "submitting"
            # LoRA：前端 lora_name 控件提交 {k}_name 与 {k}_strength
            # name 为空 = 不传该字段，后端沿用 App 发布时的默认 LoRA
            _la = str(params.get("lora_a_name") or "").strip()
            _lb = str(params.get("lora_b_name") or "").strip()
            _lora_kw = {}
            if _la:
                _lora_kw["lora_name"] = _la
                _lora_kw["lora_strength"] = float(params.get("lora_a_strength", 0.3))
            if _lb:
                _lora_kw["lora_name_b"] = _lb
                _lora_kw["lora_strength_b"] = float(params.get("lora_b_strength", 0.25))
            # rh_zimage_app.run 内部按 ref_image_path 自动选 App
            res = mod.run(
                prompt_text=prompt,
                ref_image_path=ref_path,
                width=width,
                height=height,
                steps=int(params.get("steps", 6)),
                denoise=float(params.get("denoise", 0.65)),
                download_dir=str(OUT_DIR),
                on_submit=_on_submit,
                verbose=False,
                **_lora_kw,
            )
            saved = [os.path.basename(p) for p in (res.get("saved") or [])]
            with _lock:
                TASKS[task_id].update({
                    "status": "done", "image": saved[0] if saved else None,
                    "files": saved, "pid": res.get("prompt_id"), "error": None,
                })
            _save_state()
            return
        if script_key == "illustrious_t2i":
            # Illustrious 二次元文生图（rh_illustrious_api，走 ai-app/run 通道）
            seed_s = str(params.get("seed") or "").strip()
            try:
                seed = int(seed_s) if seed_s else None
            except ValueError:
                seed = None
            def _chk(v, d=True):
                if v is None:
                    return d
                if isinstance(v, bool):
                    return v
                return str(v).strip().lower() in ("1", "true", "yes", "on")
            use_smooth = _chk(params.get("smooth"), True)
            use_cunnystyle = _chk(params.get("cunnystyle"), True)
            res_s = str(params.get("resolution") or "").strip().lower()
            _w = _h = None
            if res_s and "x" in res_s:
                try:
                    _w, _h = (int(x) for x in res_s.split("x")[:2])
                except ValueError:
                    _w = _h = None
            width = _w or 936
            height = _h or 1368
            with _lock:
                TASKS[task_id]["status"] = "submitting"
            res = mod.run(
                prompt_text=prompt,
                width=width,
                height=height,
                steps=int(params.get("steps", 20)),
                cfg=float(params.get("cfg", 2.0)),
                use_smooth_booster=use_smooth,
                smooth_strength=float(params.get("smooth_strength", 0.9)) if use_smooth else None,
                use_cunnystyle=use_cunnystyle,
                cunnystyle_strength=float(params.get("cunnystyle_strength", 0.8)) if use_cunnystyle else None,
                download_dir=str(OUT_DIR),
                on_submit=_on_submit,
            )
            saved = [os.path.basename(p) for p in (res.get("saved") or [])]
            with _lock:
                TASKS[task_id].update({
                    "status": "done", "image": saved[0] if saved else None,
                    "files": saved, "pid": str(res.get("task_id")), "error": None,
                })
            _save_state()
            return
        else:
            seed_s = str(params.get("seed") or "").strip()
            try:
                seed = int(seed_s) if seed_s else None
            except ValueError:
                seed = None
            # LoRA 开关（lora 复合控件）：style=勾选状态，style_strength=强度
            def _chk(v, d=True):
                if v is None:
                    return d
                if isinstance(v, bool):
                    return v
                return str(v).strip().lower() in ("1", "true", "yes", "on")
            use_style = _chk(params.get("style"), True)
            use_realism = _chk(params.get("realism"), True)
            use_body = _chk(params.get("body"), True)
            p = mod.build_prompt(
                prompt_text=prompt,
                model=params.get("model", "gptruth"),
                style=float(params.get("style_strength", 0.5)) if use_style else 0.0,
                realism=float(params.get("realism_strength", 0.6)) if use_realism else 0.0,
                body=float(params.get("body_strength", 0.3)) if use_body else 0.0,
                ratio=params.get("ratio", "9:16"),
                mp=float(params.get("mp", 2.0)),
                steps=int(params.get("steps", 12)),
                cfg=float(params.get("cfg", 1.0)),
                sampler=params.get("sampler", "euler"),
                scheduler=params.get("scheduler", "simple"),
                seed=seed,
                shift=float(params.get("shift", 3.0)),
                rebalance=params.get("rebalance", "detail"),
                vae=params.get("vae", "default"),
                prompt_weight=float(params.get("prompt_weight", 1.0)),
                out_format=params.get("out_format", "png"),
                use_style_lora=use_style,
                use_realism_lora=use_realism,
                use_body_lora=use_body,
            )
            with _lock:
                TASKS[task_id]["status"] = "submitting"
            pid = mod.submit(p)
            with _lock:
                TASKS[task_id]["pid"] = pid
            _save_state()
            try:
                entry = mod.poll(pid, max_wait=1800)
            except Exception as _e:
                if type(_e).__name__ == "PollTimeout":
                    with _lock:
                        TASKS[task_id]["status"] = "poll_timeout"
                        TASKS[task_id]["error"] = f"排队超时（30分钟），远端可能仍在跑。可用错误查询继续。"
                    _save_state()
                    return
                raise
            saved = []
            for f in mod.extract(entry):
                saved.append(mod.download(f, str(OUT_DIR)))
            costs = mod.get_cost(pid)
            with _lock:
                TASKS[task_id].update({
                    "status": "done", "image": saved[0] if saved else None,
                    "files": saved, "cost": costs[0] if costs else {}, "error": None,
                })
            _save_state()
            return

        with _lock:
            TASKS[task_id]["status"] = "done"
        _save_state()
    except Exception as e:
        # PollTimeout：排队超时，远端可能仍成功 → 不判死，保留 pid 等恢复
        if type(e).__name__ == "PollTimeout":
            with _lock:
                TASKS[task_id]["status"] = "poll_timeout"
                TASKS[task_id]["error"] = f"排队超时（30分钟），远端可能仍在跑。可用错误查询继续。"
            _save_state()
            return
        with _lock:
            TASKS[task_id]["status"] = "error"
            TASKS[task_id]["error"] = f"{type(e).__name__}: {e}"
        _save_state()


# ============================================================
# 错误查询 / 断点续跑（v0.4.7）
# ============================================================
def _check_pid_status(pid, mod=None):
    """一次性查远端任务状态，不阻塞。返回 {status, detail}。
    status ∈ success / error / running / queued / not_found / api_error
    两条通道：
      mod.IS_APP_CHANNEL → pid 是 ai-app/run 的 taskId，走 check_app_task（/task/openapi/outputs）
      否则 → /proxy 的 prompt_id，查 /history/<pid>，查不到再查 /queue"""
    # ---- 0. App 通道：pid 是 taskId，/proxy/history 里根本不存在 ----
    if mod is not None and getattr(mod, "IS_APP_CHANNEL", False) and hasattr(mod, "check_app_task"):
        try:
            return mod.check_app_task(pid)
        except Exception as e:
            return {"status": "api_error", "detail": str(e)}

    import urllib.request
    import urllib.error
    key = _get_api_key()
    if not key:
        return {"status": "api_error", "detail": "未配置 API Key"}

    # ---- 1. /history/<pid>：成功 / 执行失败 / 已入 history 的进行中 ----
    try:
        req = urllib.request.Request(f"https://www.runninghub.ai/proxy/{key}/history/{pid}")
        d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            d = None
        else:
            return {"status": "api_error", "detail": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "api_error", "detail": str(e)}

    if isinstance(d, dict) and pid in d:
        entry = d[pid]
        st = entry.get("status", {})
        if st.get("status_str") == "success":
            return {"status": "success", "detail": entry}
        for m in st.get("messages", []):
            if m[0] == "execution_error":
                return {"status": "error", "detail": json.dumps(m[1], ensure_ascii=False)}
        if st.get("status_str") == "error":
            return {"status": "error", "detail": "远端执行失败"}
        # history 里有但非 success/error → 进行中（已开始生成，只是还没落 history 完成态）
        return {"status": "running", "detail": entry}

    # ---- 2. history 查不到 → /queue 区分「排队中」vs「生成中」 ----
    try:
        q = json.loads(urllib.request.urlopen(
            urllib.request.Request(f"https://www.runninghub.ai/proxy/{key}/queue"),
            timeout=30).read().decode("utf-8", errors="replace"))
        running = [str(x[0]) for x in (q.get("queue_running") or [])]
        pending = [str(x[0]) for x in (q.get("queue_pending") or [])]
        if pid in running:
            return {"status": "running", "detail": {"queue": "running"}}
        if pid in pending:
            return {"status": "queued", "detail": {"queue": "pending"}}
    except Exception:
        pass

    return {"status": "not_found", "detail": "远端无此任务"}


def _download_from_entry(task_id, mod, entry, pid):
    """从 history entry 提取产物并落盘到 OUT_DIR，任务转 done"""
    saved = []
    if hasattr(mod, "extract") and hasattr(mod, "download"):
        # krea2 模块接口名
        for f in mod.extract(entry):
            saved.append(os.path.basename(mod.download(f, str(OUT_DIR))))
    else:
        extracts = mod.extract_outputs(entry)
        for out in extracts:
            p = mod.download_output(out, str(OUT_DIR))
            if p and os.path.basename(p) not in saved:
                saved.append(os.path.basename(p))
    with _lock:
        TASKS[task_id].update({"status": "done", "image": saved[0] if saved else None,
                               "files": saved, "pid": pid, "error": None})
    _save_state()
    return saved


def _claim_runaway_pid(task_id, script_key):
    """无 pid 时从 /queue + /history 认领一条不冲突的远端任务 pid。
    返回 {"ok": bool, "pid": str|None}
      ok=True  → 查询成功，pid=None 表示确实没有可认领任务
      ok=False → 查询失败（网络等），pid 未知，绝不能据此重提"""
    import urllib.request
    key = _get_api_key()
    if not key:
        return {"ok": False, "pid": None}
    used = {str(t.get("pid")) for t in TASKS.values() if t.get("pid")}
    cands = []
    probe_ok = False
    try:
        q = json.loads(urllib.request.urlopen(
            urllib.request.Request(f"https://www.runninghub.ai/proxy/{key}/queue"),
            timeout=15).read().decode("utf-8", errors="replace"))
        probe_ok = True
        for x in (q.get("queue_running") or []):
            cands.append(str(x[0]))
        for x in (q.get("queue_pending") or []):
            cands.append(str(x[0]))
    except Exception:
        pass
    try:
        h = json.loads(urllib.request.urlopen(
            urllib.request.Request(f"https://www.runninghub.ai/proxy/{key}/history"),
            timeout=15).read().decode("utf-8", errors="replace"))
        probe_ok = True
        if isinstance(h, dict):
            cands += [str(p) for p in h.keys()]
    except Exception:
        pass
    if not probe_ok:
        return {"ok": False, "pid": None}
    for pid in cands:
        if pid and pid not in used and pid.lower() != "none":
            return {"ok": True, "pid": pid}
    return {"ok": True, "pid": None}


def _resume_poll(task_id, mod, pid):
    """续轮询线程：阻塞等 success（最长 30 分钟），成功即落盘；超时转 poll_timeout"""
    try:
        _inject_api_key(mod)
        with _lock:
            TASKS[task_id]["status"] = "submitting"
            TASKS[task_id]["error"] = None
        _save_state()
        if hasattr(mod, "poll"):
            entry = mod.poll(pid, max_wait=1800)
        else:
            entry = mod.poll_history(pid, max_wait=1800)
        _download_from_entry(task_id, mod, entry, pid)
    except Exception as e:
        if type(e).__name__ == "PollTimeout":
            with _lock:
                TASKS[task_id]["status"] = "poll_timeout"
                TASKS[task_id]["error"] = "排队超时，远端可能仍在跑。可再次错误查询。"
            _save_state()
        else:
            with _lock:
                TASKS[task_id]["status"] = "error"
                TASKS[task_id]["error"] = f"{type(e).__name__}: {e}"
            _save_state()


def _inspect_task(task_id):
    """错误查询总调度：有 pid 查远端 / 无 pid 自动认领 / 认领不到自动重提。全程只点一次。"""
    with _lock:
        if task_id not in TASKS:
            return {"ok": False, "error": f"任务不存在: {task_id}"}
        t = TASKS[task_id]
        script_key = t.get("script") or ""
        params = t.get("params") or {}
        pid = t.get("pid")
    if script_key not in REGISTRY:
        return {"ok": False, "error": f"脚本不可用: {script_key}"}
    mod = _load_module(REGISTRY[script_key]["module"])
    _inject_api_key(mod)        # 手动错误查询同样要先注入 Key，否则 301 must not be blank

    # ---- 1. 有 pid：查远端 ----
    if pid:
        st = _check_pid_status(pid, mod)
        if st["status"] == "success":
            try:
                saved = _download_from_entry(task_id, mod, st["detail"], pid)
                return {"ok": True, "action": "pulled", "pid": pid, "saved": saved,
                        "msg": f"远端已完成，已拉回 {len(saved)} 个产物"}
            except Exception as e:
                return {"ok": False, "error": f"落盘失败: {e}"}
        if st["status"] == "error":
            with _lock:
                TASKS[task_id].update({
                    "status": "error",
                    "error": f"远端执行失败: {st['detail']}",
                    "resubmit_pending": True,
                })
            _save_state()
            return {"ok": True, "action": "remote_error", "pid": pid, "detail": st["detail"],
                    "resubmit_pending": True,
                    "msg": "远端已失败，是否需要重新提交？"}
        if st["status"] in ("pending", "running", "queued"):
            threading.Thread(target=_resume_poll, args=(task_id, mod, pid), daemon=True).start()
            _what = {"pending": "生成中", "running": "生成中", "queued": "排队中"}.get(st["status"], "进行中")
            return {"ok": True, "action": "resumed", "pid": pid,
                    "msg": f"远端{_what}，已恢复轮询，出图自动落盘"}
        if st["status"] == "api_error":
            # 查询失败（网络/超时），任务状态未知 → 绝不重提，提示稍后重试
            return {"ok": False, "error": f"查询远端失败（{st['detail']}），请稍后重试", "retryable": True}
        # st["status"] == "not_found" → 远端确实没有此 pid，尝试认领
        pid = None

    # ---- 2. 无 pid：自动认领 ----
    if not pid:
        # App 通道没有 /proxy/queue，无法按队列认领。
        # 但「远端确认无此任务」同样要走第 3 步：挂起询问让用户决定是否重提，
        # 而不是把任务永远卡在 submitting。
        if not getattr(mod, "IS_APP_CHANNEL", False):
            claim = _claim_runaway_pid(task_id, script_key)
            if not claim["ok"]:
                # 认领查询也失败 → 状态未知，绝不重提
                return {"ok": False, "error": "查询远端队列失败，请稍后重试", "retryable": True}
            claimed = claim["pid"]
            if claimed:
                with _lock:
                    TASKS[task_id]["pid"] = claimed
                _save_state()
                st = _check_pid_status(claimed, mod)
                if st["status"] == "success":
                    try:
                        saved = _download_from_entry(task_id, mod, st["detail"], claimed)
                        return {"ok": True, "action": "claimed_pulled", "pid": claimed, "saved": saved,
                                "msg": f"自动认领远端任务并拉回 {len(saved)} 个产物"}
                    except Exception as e:
                        return {"ok": False, "error": f"落盘失败: {e}"}
                if st["status"] == "api_error":
                    return {"ok": False, "error": f"查询认领任务失败（{st['detail']}），请稍后重试", "retryable": True}
                threading.Thread(target=_resume_poll, args=(task_id, mod, claimed), daemon=True).start()
                return {"ok": True, "action": "claimed_resumed", "pid": claimed,
                        "msg": "已自动认领远端任务并恢复轮询，出图自动落盘"}

    # ---- 3. 认领不到：不自动重提，挂起询问让用户决定 ----
    with _lock:
        TASKS[task_id].update({
            "status": "error",
            "error": "远端无此任务（可能已过期或被清理），是否重新提交？",
            "resubmit_pending": True,
        })
    _save_state()
    return {"ok": True, "action": "resubmit_ask", "resubmit_pending": True,
            "msg": "远端已无此任务，需要你确认后才会重新提交（会消耗点数）"}


def _resubmit_task(task_id):
    """用户确认后重新提交：清掉 resubmit_pending，重置状态，起后台线程重跑。
    只有人点了「确认重提」才会走到这里（会消耗点数）。"""
    with _lock:
        if task_id not in TASKS:
            return {"ok": False, "error": f"任务不存在: {task_id}"}
        t = TASKS[task_id]
        script_key = t.get("script") or ""
        params = t.get("params") or {}
        if not params:
            return {"ok": False, "error": "任务缺少参数，无法重提，请用「复用」重新填表提交"}
        t.update({"status": "queued", "pid": None, "error": None,
                  "resubmit_pending": False, "files": [], "image": None})
    _save_state()
    ret_key = script_key
    ret_params = dict(params)
    threading.Thread(target=_run_task, args=(task_id, ret_key, ret_params), daemon=True).start()
    return {"ok": True, "action": "resubmitted", "msg": "已重新提交，新任务号会自动接管"}


def _dismiss_resubmit(task_id):
    """用户选择不重提：只清掉询问标记，任务保持 error"""
    with _lock:
        if task_id not in TASKS:
            return {"ok": False, "error": f"任务不存在: {task_id}"}
        TASKS[task_id]["resubmit_pending"] = False
    _save_state()
    return {"ok": True, "action": "dismissed", "msg": "已保留失败状态，未重新提交"}


def _has_active_task():
    """是否有正在运行的任务（queued/submitting/running/poll_timeout）。
    普通账户一次只能跑一个任务，提交入口据此拒绝并发。"""
    with _lock:
        for t in TASKS.values():
            if t.get("status") in ("queued", "submitting", "running", "poll_timeout"):
                return True
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 轻量请求日志：只打 /v1/* 和非静态接口，避免刷屏
        try:
            p = self.path or ""
            if p.startswith("/v1/") or (p.startswith("/api/") and not p.startswith("/api/tasks")):
                msg = fmt % args
                print(f"[req] {self.command} {p} {msg.strip()}", flush=True)
        except Exception:
            pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_file(self, fp):
        """按扩展名推断 MIME 发一个文件（带 CORS）。
        支持 Range（206）：视频缩略图/拖动进度依赖它，否则每次都全量下载。"""
        low = fp.name.lower()
        if low.endswith(".mp4"):
            ctype = "video/mp4"
        elif low.endswith(".webm"):
            ctype = "video/webm"
        elif low.endswith(".png"):
            ctype = "image/png"
        elif low.endswith(".jpg") or low.endswith(".jpeg"):
            ctype = "image/jpeg"
        elif low.endswith(".webp"):
            ctype = "image/webp"
        else:
            ctype = "application/octet-stream"
        data = fp.read_bytes()
        rng = self.headers.get("Range")
        start, end = 0, len(data)
        if rng:
            try:
                unit, _, spec = rng.strip().partition("=")
                if unit.lower() == "bytes":
                    s, _, e = spec.strip().partition("-")
                    if s:
                        start = int(s)
                        end = int(e) + 1 if e else len(data)
                    elif e:                     # bytes=-N（尾部 N 字节）
                        start = max(0, len(data) - int(e))
                    start = max(0, min(start, len(data)))
                    end = max(start, min(end, len(data)))
            except Exception:
                start, end = 0, len(data)
        chunk = data[start:end]
        if rng and start < len(data):
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end - 1}/{len(data)}")
        elif rng:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges")
        self.end_headers()
        self.wfile.write(chunk)

    def _is_local_req(self):
        """判断请求是否来自本机。
        /api/* 是 WebUI 自用接口（含读写配置、消耗用户自有 LLM Key 的润色），
        只该由本机页面调用。电脑版监听 0.0.0.0，若不校验，同内网任何人都能
        白嫖用户自己填的 LLM Key、读改配置。App 内嵌版绑 127.0.0.1 天然安全。
        """
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            return False
        return ip in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")

    def _deny_nonlocal(self):
        """/api/* 非本机来源直接拒绝。返回 True 表示已拒绝。"""
        if self._is_local_req():
            return False
        self._send(403, {"error": "该接口仅限本机访问（WebUI 自用配置接口）"})
        return True

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        import rh_openai  # OpenAI 兼容层（/v1/*），函数级导入避免局部变量未绑定
        # /api/* 仅本机；/v1/* 走 openai_key 鉴权；静态资源不拦
        if path.startswith("/api/") and self._deny_nonlocal():
            return
        if path == "/" or path == "/index.html":
            html = HTML_FILE.read_text(encoding="utf-8") if HTML_FILE.exists() else "<h1>webui.html 缺失</h1>"
            # 单源注入：HTML 里 __API_PORT__ 占位符替换为实际监听端口（本地 8770 / app 8765）
            try:
                html = html.replace("__API_PORT__", str(self.server.server_address[1]))
            except Exception:
                pass
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/scripts":
            self._send(200, {k: {"name": v["name"], "desc": v["desc"], "mode": v["mode"], "params": v["params"]}
                             for k, v in REGISTRY.items()})
        elif path == "/api/refs":
            self._send(200, {"refs": _list_refs(), "outputs": _list_outputs()})
        elif path == "/api/balance":
            self._send(200, _query_balance())
        elif path == "/api/market":
            q = parse_qs(u.query)
            page = int((q.get("page") or ["1"])[0])
            size = int((q.get("size") or ["12"])[0])
            tag = (q.get("tag") or [None])[0]
            kw = (q.get("kw") or [None])[0]
            sort = (q.get("sort") or [None])[0]
            self._send(200, _market_list(page=page, size=size, tag=tag, keyword=kw, sort=sort))
        elif path == "/api/market/tags":
            self._send(200, _market_tags())
        elif path == "/api/market/detail":
            q = parse_qs(u.query)
            wid = (q.get("id") or [""])[0]
            if not wid:
                self._send(400, {"error": "缺少 id"})
                return
            self._send(200, _market_detail(wid))
        elif path == "/api/settings":
            key = _get_api_key()
            llm = _get_llm_cfg()
            oa_key = _load_config().get("openai_key") or ""
            try:
                _port = self.server.server_address[1]
            except Exception:
                _port = RUNNING_PORT
            try:
                import rh_openai
                _lan = rh_openai.list_lan_ips()
            except Exception:
                _lan = []
            self._send(200, {
                "api_key_set": bool(key), "api_key_masked": _mask(key),
                "llm": {
                    "base_url": llm["base_url"],
                    "model": llm["model"],
                    "api_key_set": bool(llm["api_key"]),
                    "api_key_masked": _mask(llm["api_key"]),
                    "style": llm["style"],
                },
                "openai": {
                    "api_key_set": bool(oa_key),
                    "api_key_masked": _mask(oa_key),
                    "base_url_local": f"http://127.0.0.1:{_port}/v1",
                    "base_url_lan": [f"http://{ip}:{_port}/v1" for ip in _lan],
                    "model": "zimage",
                },
            })
        elif path == "/api/openai_key":
            # 返回完整 key（本机接口，仅用于复制给第三方软件）
            oa_key = _load_config().get("openai_key") or ""
            self._send(200, {"ok": True, "key": oa_key})
        elif path == "/api/backend":
            self._send(200, _backend_status())
        elif path == "/api/update_check":
            q = parse_qs(u.query)
            force = (q.get("force") or ["0"])[0] == "1"
            self._send(200, _update_check(force=force))
        elif path == "/api/tasks":
            with _lock:
                t = {}
                for _k, _v in TASKS.items():
                    e = {kk: vv for kk, vv in _v.items() if kk != "trace"}
                    # 磁盘才是事实来源：产物可能已被删除（素材管理/外部清理），
                    # 不校验会让前端拿着失效文件名渲染成破图占位。
                    files = [f for f in (e.get("files") or [])
                             if f and (OUT_DIR / os.path.basename(str(f))).exists()]
                    img = e.get("image")
                    if img and not (OUT_DIR / os.path.basename(str(img))).exists():
                        img = None
                    e["files"] = files
                    e["image"] = img or (files[0] if files else None)
                    t[_k] = e
            self._send(200, {"tasks": t})
        elif path == "/api/latest_image":
            # 最近一张完成的出图（status=done 且 ts 最新，文件必须真实落盘）
            with _lock:
                best_name, best_ts = None, -1
                for _t in TASKS.values():
                    if _t.get("status") != "done" or not _t.get("image"):
                        continue
                    nm = os.path.basename(str(_t["image"]))
                    if not (OUT_DIR / nm).exists():
                        continue
                    ts = _t.get("ts") or 0
                    if ts > best_ts:
                        best_ts, best_name = ts, nm
            if best_name:
                self._send(200, {"ok": True, "name": best_name})
            else:
                self._send(200, {"ok": True, "name": None})
        elif path.startswith("/refs/"):
            # refs: 素材 / refs/out: 产物 均可访问
            raw = unquote(path[len("/refs/"):])
            if raw.startswith("out:"):
                name = os.path.basename(raw[4:])
                fp = OUT_DIR / name
                if not fp.exists():
                    self._send(404, {"error": "not found"})
                    return
                return self._send_file(fp)
            name = os.path.basename(raw)
            fp = REFS_DIR / name
            if not fp.exists():
                self._send(404, {"error": "not found"})
                return
            return self._send_file(fp)
        elif path.startswith("/images/"):
            name = os.path.basename(unquote(path))
            fp = OUT_DIR / name
            if not fp.exists():
                self._send(404, {"error": "not found"})
                return
            return self._send_file(fp)
        elif path == "/v1/models":
            try:
                port = self.server.server_address[1]
            except Exception:
                port = RUNNING_PORT
            self._send(200, rh_openai.models(port))
        elif path.startswith("/v1/images/generations/"):
            tid = os.path.basename(path)
            code, resp = rh_openai.task_status(tid)
            self._send(code, resp)
        elif path.startswith("/v1/files/"):
            name = os.path.basename(unquote(path))
            # 主目录是 webui_out（与历史共用）；openai_out 兼容改造前老的产物
            cand = [rh_openai._out_dir() / name, OUT_DIR / name]
            fp = next((c for c in cand if c.exists()), None)
            if not fp:
                self._send(404, {"error": "not found"})
                return
            return self._send_file(fp)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        import rh_openai  # OpenAI 兼容层（/v1/*），函数级导入避免局部变量未绑定
        # /api/* 仅本机（含润色会消耗用户自有 LLM Key、可读写配置）
        if path.startswith("/api/") and self._deny_nonlocal():
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        if path == "/api/market/submit":
            if _has_active_task():
                self._send(400, {"error": "已有任务在运行，请等待完成后再提交"})
                return
            try:
                data = json.loads(raw.decode() or "{}")
            except Exception:
                self._send(400, {"error": "bad json"})
                return
            wid = str(data.get("webapp_id") or "").strip()
            vals = data.get("node_values") or {}
            wh = str(data.get("webhook") or "").strip() or None
            it = str(data.get("instance_type") or "default").strip() or "default"
            if not wid:
                self._send(400, {"error": "缺少 webapp_id"})
                return
            r = _market_submit(wid, vals, webhook=wh, instance_type=it)
            self._send(200, r)
        elif path == "/api/submit":
            if _has_active_task():
                self._send(400, {"error": "已有任务在运行，请等待完成后再提交"})
                return
            try:
                data = json.loads(raw.decode() or "{}")
            except Exception:
                self._send(400, {"error": "bad json"})
                return
            script_key = data.get("script")
            if script_key not in REGISTRY:
                self._send(400, {"error": f"未知脚本 {script_key}"})
                return
            with _lock:
                TASK_SEQ[0] += 1
                tid = f"T{TASK_SEQ[0]:03d}_{int(time.time())}"
                TASKS[tid] = {"script": script_key, "params": data.get("params", {}),
                              "status": "queued", "pid": None, "image": None,
                              "files": [], "cost": {}, "error": None, "ts": time.time()}
            threading.Thread(target=_run_task, args=(tid, script_key, data.get("params", {})),
                             daemon=True).start()
            _save_state()
            self._send(200, {"task_id": tid})
        elif path == "/v1/images/generations":
            try:
                body = json.loads(raw.decode() or "{}")
            except Exception:
                self._send(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
                return
            headers = {k: v for k, v in self.headers.items()}
            host = self.headers.get("Host", f"127.0.0.1:{RUNNING_PORT}")
            code, resp = rh_openai.generate(body, headers, host)
            self._send(code, resp)
        elif path == "/v1/chat/completions":
            # 不提供对话能力：透传会把用户本机私有 LLM Key 借给任何持有 openai_key 的人
            headers = {k: v for k, v in self.headers.items()}
            code, resp = rh_openai.chat_completions(raw, headers)
            self._send(code, resp)
        elif path == "/api/polish":
            try:
                data = json.loads(raw.decode() or "{}")
                llm = _get_llm_cfg()
                if not (llm["base_url"] and llm["model"] and llm["api_key"]):
                    self._send(400, {"error": "请先在 ⚙ 设置中配置 LLM（Base URL / Key / 模型名）"})
                    return
                prompt_text = (data.get("prompt") or "").strip()
                if not prompt_text:
                    self._send(400, {"error": "提示词为空"})
                    return
                kind = str(data.get("kind") or "t2i")
                polished = _polish_prompt(prompt_text, kind, llm)
                self._send(200, {"ok": True, "polished": polished})
            except urllib.error.HTTPError as e:
                try:
                    detail = json.loads(e.read().decode() or "{}")
                    msg = detail.get("error", {}).get("message") or str(e)
                except Exception:
                    msg = str(e)
                self._send(502, {"error": f"LLM 接口错误: {msg}"})
            except Exception as e:
                self._send(500, {"error": f"润色失败: {e}"})
        elif path == "/api/settings":
            try:
                data = json.loads(raw.decode() or "{}")
                cfg = _load_config()
                # RunningHub key
                if "api_key" in data:
                    key = str(data.get("api_key") or "").strip()
                    if not key:
                        # 空串不覆盖已存 key（避免误清空）
                        pass
                    elif not re.fullmatch(r"[A-Za-z0-9]{16,64}", key):
                        self._send(400, {"error": "密钥格式不对（应为 RunningHub API Key，16-64 位字母数字）"})
                        return
                    else:
                        cfg["api_key"] = key
                # LLM 配置（OpenAI 兼容，任意字段可单独更新，空串=清除）
                for fld in ("llm_base_url", "llm_model", "llm_api_key", "llm_style"):
                    if fld in data:
                        cfg[fld] = str(data.get(fld) or "").strip()
                # OpenAI 兼容层密钥：空串不覆盖；regen=重新生成
                if data.get("openai_regen"):
                    cfg["openai_key"] = "sk-" + uuid.uuid4().hex[:32]
                elif "openai_key" in data:
                    ok_ = str(data.get("openai_key") or "").strip()
                    if ok_:
                        cfg["openai_key"] = ok_
                _save_config(cfg)
                resp = {"ok": True}
                if data.get("openai_regen") or "openai_key" in data:
                    resp["openai_key"] = cfg.get("openai_key") or ""
                self._send(200, resp)
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/upload":
            ct = self.headers.get("Content-Type", "")
            try:
                boundary = ct.split("boundary=")[1].strip().strip('"')
                parts = raw.split(b"--" + boundary.encode())
                fname, content = None, b""
                for part in parts:
                    if b"filename=" in part:
                        head, _, body = part.partition(b"\r\n\r\n")
                        fname = head.split(b'filename="')[1].split(b'"')[0].decode()
                        content = body.rsplit(b"\r\n", 1)[0]
                        break
                if not fname:
                    self._send(400, {"error": "no file"})
                    return
                REFS_DIR.mkdir(parents=True, exist_ok=True)
                # 同名防覆盖：IMG_0001.jpg → IMG_0001 (2).jpg
                stem, dot, ext = fname.rpartition(".")
                if not dot:                      # 无扩展名
                    stem, ext = fname, ""
                cand = f"{stem}.{ext}" if ext else stem
                n = 1
                while (REFS_DIR / cand).exists():
                    n += 1
                    cand = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
                (REFS_DIR / cand).write_bytes(content)
                self._send(200, {"ok": True, "name": cand})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/refs/delete":
            try:
                data = json.loads(raw.decode() or "{}")
                name = str(data.get("name") or "").strip()
                if not name or "/" in name or "\\" in name:
                    self._send(400, {"error": "bad name"})
                    return
                if name.startswith("out:"):
                    target = OUT_DIR / os.path.basename(name[4:])
                    if target.exists():
                        target.unlink()
                        self._send(200, {"ok": True, "name": name})
                        return
                    self._send(404, {"error": "not found"})
                    return
                target = REFS_DIR / os.path.basename(name)
                if not target.exists():
                    self._send(404, {"error": "not found"})
                    return
                target.unlink()
                self._send(200, {"ok": True, "name": name})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/task/export":
            try:
                data = json.loads(raw.decode() or "{}")
                tid = str(data.get("id") or "").strip()
                with _lock:
                    t = TASKS.get(tid)
                if not t:
                    self._send(404, {"error": f"任务不存在: {tid}"})
                    return
                files = t.get("files") or []
                if t.get("image") and t["image"] not in files:
                    files = [t["image"]] + list(files)
                if not files:
                    self._send(400, {"error": "该任务没有产物文件"})
                    return
                done = _export_to_public(files)
                if not done:
                    self._send(400, {"error": "导出失败（可能非安卓环境或 MediaStore 不可用）"})
                    return
                self._send(200, {"ok": True, "exported": done})
            except Exception as e:
                self._send(500, {"error": f"导出失败: {e}"})
        elif path == "/api/task/lastframe":
            try:
                data = json.loads(raw.decode() or "{}")
                tid = str(data.get("id") or "").strip()
                ok, res = _task_lastframe(tid)
                self._send(200 if ok else 400, res)
            except Exception as e:
                self._send(500, {"error": f"抽帧失败: {e}"})
        elif path == "/api/task/inspect":
            try:
                data = json.loads(raw.decode() or "{}")
                tid = str(data.get("id") or "").strip()
                if not tid:
                    self._send(400, {"error": "缺少任务 id"})
                    return
                r = _inspect_task(tid)
                self._send(200, r)
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, {"error": f"错误查询失败: {e}"})
        elif path == "/api/task/resubmit":
            try:
                data = json.loads(raw.decode() or "{}")
                tid = str(data.get("id") or "").strip()
                if not tid:
                    self._send(400, {"error": "缺少任务 id"})
                    return
                if _has_active_task():
                    self._send(400, {"error": "已有任务在运行，请等待完成后再重提"})
                    return
                self._send(200, _resubmit_task(tid))
            except Exception as e:
                self._send(500, {"error": f"重提失败: {e}"})
        elif path == "/api/task/resubmit/dismiss":
            try:
                data = json.loads(raw.decode() or "{}")
                tid = str(data.get("id") or "").strip()
                if not tid:
                    self._send(400, {"error": "缺少任务 id"})
                    return
                self._send(200, _dismiss_resubmit(tid))
            except Exception as e:
                self._send(500, {"error": f"操作失败: {e}"})
        elif path == "/api/task/delete":
            try:
                data = json.loads(raw.decode() or "{}")
                tid = str(data.get("id") or "").strip()
                if not tid:
                    self._send(400, {"error": "缺少任务 id"})
                    return
                with _lock:
                    if tid not in TASKS:
                        self._send(404, {"error": f"任务不存在: {tid}"})
                        return
                    del TASKS[tid]
                _save_state()
                self._send(200, {"ok": True, "id": tid})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/clear":
            with _lock:
                TASKS.clear()
            _save_state()
            self._send(200, {"ok": True})
        elif path == "/api/backend/restart":
            self._send(200, _restart_backend())
            # 返回响应后延迟重启：spawn 新进程（独立会话）再退出旧进程，保证新进程存活
            def _do_restart():
                time.sleep(0.8)
                try:
                    import subprocess as _sp
                    # Chaquopy 环境没有独立 python 解释器，不能 spawn；提示用户重开 app
                    if os.environ.get("ANDROID_ROOT") or "chaquopy" in str(sys.prefix).lower():
                        print("[android] 重启后端请重新打开 App（Chaquopy 内嵌环境不支持进程替换）")
                        return
                    log_f = open(str(BASE / "workflows" / "webui_server.log"), "ab")
                    args = [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]
                    _sp.Popen(args, stdout=log_f, stderr=log_f,
                              start_new_session=True, cwd=str(BASE))
                    log_f.close()
                except Exception:
                    pass
                try:
                    if not (os.environ.get("ANDROID_ROOT") or "chaquopy" in str(sys.prefix).lower()):
                        os._exit(0)
                except Exception:
                    pass
            threading.Thread(target=_do_restart, daemon=True).start()
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="RunningHub 跑图 WebUI")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    RUNNING_PORT = a.port
    print(f"洛宝跑图台: http://localhost:{a.port}")
    print(f"  refs: {REFS_DIR} | 输出: {OUT_DIR}")
    _load_state()  # BASE 已确定，恢复历史任务
    _recover_stale_tasks()  # 自动复核上次进程遗留的『假活』任务
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
