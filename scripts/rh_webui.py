#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RunningHub 跑图 WebUI 服务端（页面在 webui.html）"""

import json, os, sys, time, threading, traceback, importlib, re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE / "scripts"
REFS_DIR = BASE / "refs"
OUT_DIR = BASE / "workflows" / "webui_out"
HTML_FILE = BASE / "webui.html"
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
        body.setdefault("extra", {})["keyword"] = keyword
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
            "ok": True, "pid": os.getpid(), "port": PORT,
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


START_TIME = time.time()
_RESTART_FLAG = [False]


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
    for mname in ("rh_zimage_api", "rh_krea2_nsfw", "rh_h3_flf_api", "rh_workflow_api"):
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
    """启动时从 webui_state.json 恢复历史任务（跨重启保留）"""
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


_load_state()

REGISTRY = {
    "zimage_i2i": {
        "name": "Zimage 图生图/文生图 (I2I+T2I)",
        "desc": "参考图换人/换姿势（选图=图生图）/ 纯提示词生图（不选图=文生图）。主力方案",
        "module": "rh_zimage_api", "mode": "i2i",
        "params": [
            {"k": "resolution", "label": "分辨率", "type": "select", "options": [
                {"v": "768x1368", "label": "768×1368（竖屏 9:16 · 省币）"},
                {"v": "1080x1920", "label": "1080×1920（竖屏 9:16 · 推荐）"},
                {"v": "1242x2688", "label": "1242×2688（竖屏 9:16 · 高清）"},
                {"v": "1024x1024", "label": "1024×1024（方形 1:1）"},
                {"v": "1368x768", "label": "1368×768（横屏 16:9 · 省币）"},
                {"v": "1920x1080", "label": "1920×1080（横屏 16:9 · 高清）"},
            ], "def": "1080x1920"},
            {"k": "steps", "label": "步数", "type": "int", "def": 12},
            {"k": "denoise", "label": "重绘强度", "type": "float", "def": 0.7, "help": "0.5-0.6锁姿势 / 0.7平衡 / 0.85+大幅改写"},
            {"k": "lora", "label": "LoRA", "type": "select", "options": ["none", "charm", "nofilter", "flowdpo", "model", "peach"], "def": "none"},
            {"k": "lora_strength", "label": "LoRA强度", "type": "float", "def": 0.7, "help": "仅 LoRA≠none 时生效"},
            {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机", "group": "advanced"},
        ],
    },
    "krea2_t2i": {
        "name": "Krea2 文生图 (T2I)",
        "desc": "纯文生图，Krea2 底模，支持权重语法 (词:1.5)",
        "module": "rh_krea2_nsfw", "mode": "t2i",
        "params": [
            {"k": "model", "label": "底模", "type": "select", "options": [{"v": "gptruth", "label": "GPTruth（写实向，推荐）"}, {"v": "nsfw", "label": "NSFW V4（能出裸体）"}, {"v": "redcraft23", "label": "RedCraft23（工作流默认）"}, {"v": "redmix", "label": "RedMix（写实混合）"}, {"v": "moody", "label": "Moody（氛围向）"}, {"v": "muse", "label": "Muse（艺术向）"}, {"v": "turbo", "label": "Turbo（加速模型，快但略降质）"}], "def": "gptruth"},
            {"k": "ratio", "label": "比例", "type": "select", "options": ["9:16", "16:9", "1:1", "2:3", "3:2", "3:4", "4:3", "21:9"], "def": "9:16"},
            {"k": "mp", "label": "分辨率", "type": "select", "options": [{"v": "0.5", "label": "0.5 MP（低清快出）"}, {"v": "1.0", "label": "1 MP（标清）"}, {"v": "2.0", "label": "2 MP ≈1080×1920（推荐）"}, {"v": "4.0", "label": "4 MP（2K级）"}, {"v": "8.0", "label": "8 MP（超高清）"}], "def": "2.0", "help": "2 MP 约等于 1080×1920 竖屏，越大越贵越慢"},
            {"k": "steps", "label": "步数", "type": "int", "def": 12},
            {"k": "style", "label": "风格LoRA", "type": "float", "def": 0.5},
            {"k": "realism", "label": "写实LoRA", "type": "float", "def": 0.6},
            {"k": "body", "label": "身材LoRA", "type": "float", "def": 0.3},
            {"k": "prompt_weight", "label": "PromptWeight", "type": "float", "def": 1.0, "help": "0=禁用；>0 启用 (词:1.5) 权重语法"},
            {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机", "group": "advanced"},
            {"k": "cfg", "label": "CFG", "type": "float", "def": 1.0, "help": "Krea2 建议 1.0", "group": "advanced"},
            {"k": "sampler", "label": "采样器", "type": "select", "options": ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_sde", "uni_pc"], "def": "euler", "group": "advanced"},
            {"k": "scheduler", "label": "调度器", "type": "select", "options": ["simple", "normal", "karras", "sgm_uniform"], "def": "simple", "group": "advanced"},
            {"k": "shift", "label": "AuraFlow shift", "type": "float", "def": 3.0, "group": "advanced"},
            {"k": "rebalance", "label": "Rebalance", "type": "select", "options": ["balanced", "detail", "subtle", "uniform", "custom"], "def": "detail", "group": "advanced"},
            {"k": "vae", "label": "VAE", "type": "select", "options": ["default", "hdr", "sharp", "hd"], "def": "default", "help": "hdr 磨皮严重，default 保细节", "group": "advanced"},
            {"k": "out_format", "label": "输出格式", "type": "select", "options": ["png", "jpg"], "def": "png", "group": "advanced"},
        ],
    },
    "krea2_i2i": {
        "name": "Krea2 图生图 (I2I)",
        "desc": "Krea2 + 参考图注入 (LoadImage→VAEEncode)，保持角色改姿势",
        "module": "rh_krea2_nsfw", "mode": "i2i",
        "params": [
            {"k": "model", "label": "底模", "type": "select", "options": [{"v": "gptruth", "label": "GPTruth（写实向，推荐）"}, {"v": "nsfw", "label": "NSFW V4（能出裸体）"}, {"v": "redcraft23", "label": "RedCraft23（工作流默认）"}, {"v": "redmix", "label": "RedMix（写实混合）"}, {"v": "moody", "label": "Moody（氛围向）"}, {"v": "muse", "label": "Muse（艺术向）"}, {"v": "turbo", "label": "Turbo（加速模型，快但略降质）"}], "def": "gptruth"},
            {"k": "ratio", "label": "比例", "type": "select", "options": ["9:16", "16:9", "1:1", "2:3", "3:2", "3:4", "4:3", "21:9"], "def": "9:16"},
            {"k": "mp", "label": "分辨率", "type": "select", "options": [{"v": "0.5", "label": "0.5 MP（低清快出）"}, {"v": "1.0", "label": "1 MP（标清）"}, {"v": "2.0", "label": "2 MP ≈1080×1920（推荐）"}, {"v": "4.0", "label": "4 MP（2K级）"}, {"v": "8.0", "label": "8 MP（超高清）"}], "def": "2.0", "help": "2 MP 约等于 1080×1920 竖屏，越大越贵越慢"},
            {"k": "steps", "label": "步数", "type": "int", "def": 12},
            {"k": "denoise", "label": "重绘强度", "type": "float", "def": 0.65},
            {"k": "style", "label": "风格LoRA", "type": "float", "def": 0.5},
            {"k": "realism", "label": "写实LoRA", "type": "float", "def": 0.6},
            {"k": "body", "label": "身材LoRA", "type": "float", "def": 0.3},
            {"k": "seed", "label": "随机种子", "type": "text", "def": "", "placeholder": "留空 = 随机", "group": "advanced"},
            {"k": "cfg", "label": "CFG", "type": "float", "def": 1.0, "help": "Krea2 建议 1.0", "group": "advanced"},
            {"k": "sampler", "label": "采样器", "type": "select", "options": ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_sde", "uni_pc"], "def": "euler", "group": "advanced"},
            {"k": "scheduler", "label": "调度器", "type": "select", "options": ["simple", "normal", "karras", "sgm_uniform"], "def": "simple", "group": "advanced"},
            {"k": "shift", "label": "AuraFlow shift", "type": "float", "def": 3.0, "group": "advanced"},
            {"k": "rebalance", "label": "Rebalance", "type": "select", "options": ["balanced", "detail", "subtle", "uniform", "custom"], "def": "detail", "group": "advanced"},
            {"k": "vae", "label": "VAE", "type": "select", "options": ["default", "hdr", "sharp", "hd"], "def": "default", "help": "hdr 磨皮严重，default 保细节", "group": "advanced"},
            {"k": "out_format", "label": "输出格式", "type": "select", "options": ["png", "jpg"], "def": "png", "group": "advanced"},
        ],
    },
    "h3_flf_video": {
        "name": "MiniMaxH3 首尾帧视频 (I2V)",
        "desc": "首帧图(→尾帧图) → 视频。支持单段/续接，输出 mp4",
        "module": "rh_h3_flf_api", "mode": "video",
        "params": [
            {"k": "first_ref", "label": "首帧图", "type": "ref", "def": ""},
            {"k": "last_ref", "label": "尾帧图(可选)", "type": "ref", "def": ""},
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
            {"k": "ref_image", "label": "参考图(可选)", "type": "ref", "def": ""},
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
}


def _load_module(name):
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)


def _list_refs():
    if not REFS_DIR.exists():
        return []
    return sorted([p.name for p in REFS_DIR.iterdir()
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")])


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
    try:
        Path(BASE / "workflows").mkdir(parents=True, exist_ok=True)
        with open(BASE / "workflows" / "webui_state.json", "w") as f:
            json.dump({k: {kk: vv for kk, vv in v.items() if kk != "trace"}
                       for k, v in TASKS.items()}, f, ensure_ascii=False)
    except Exception:
        pass


def _run_task(task_id, script_key, params):
    cfg = REGISTRY[script_key]
    try:
        mod = _load_module(cfg["module"])
        # 注入用户配置的 API Key（替代脚本内写死的 key）
        _inject_api_key(mod)
        # krea2_i2i 分支会 import rh_zimage_api 做参考图上传，也要注入
        if script_key == "krea2_i2i":
            try:
                import rh_zimage_api as _Z
                _inject_api_key(_Z)
            except Exception:
                pass
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
                )
                saved = res.get("saved", []) or []
                # omni 工作流输出可能混入中间节点 png，image 优先选 mp4
                _img = next((f for f in saved if str(f).lower().endswith((".mp4", ".webm", ".mov"))), saved[0] if saved else None)
                with _lock:
                    TASKS[task_id].update({"status": "done", "image": _img,
                                           "files": saved, "pid": res.get("prompt_id"), "error": None})
            _save_state()
            return

        if script_key == "zimage_i2i":
            ref = params.get("ref_image") or ""
            # 有参考图 → 图生图（支持 refs/ 与 out: 前缀）；无 → 自动切文生图
            ref_path = _resolve_ref_path(ref)
            lora = params.get("lora") or None
            if lora == "none":
                lora = None
            seed_s = str(params.get("seed") or "").strip()
            try:
                seed = int(seed_s) if seed_s else None
            except ValueError:
                seed = None
            # 分辨率：新下拉 "宽x高"，回退旧的 width/height 字段（历史任务重跑兼容）
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
            res = mod.run(
                prompt_text=prompt,
                ref_image_path=ref_path,
                width=width,
                height=height,
                steps=int(params.get("steps", 12)),
                denoise=float(params.get("denoise", 0.7)),
                seed=seed,
                lora=lora, lora_strength=float(params.get("lora_strength", 0.7)) if lora else None,
                download_dir=str(OUT_DIR),
            )
            saved = [os.path.basename(p) for p in (res.get("saved") or [])]
            with _lock:
                TASKS[task_id].update({
                    "status": "done", "image": saved[0] if saved else None,
                    "files": saved, "pid": res.get("prompt_id"), "error": None,
                })
            _save_state()
            return
        else:
            seed_s = str(params.get("seed") or "").strip()
            try:
                seed = int(seed_s) if seed_s else None
            except ValueError:
                seed = None
            p = mod.build_prompt(
                prompt_text=prompt,
                model=params.get("model", "gptruth"),
                style=float(params.get("style", 0.5)),
                realism=float(params.get("realism", 0.6)),
                body=float(params.get("body", 0.3)),
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
            )
            if script_key == "krea2_i2i":
                ref = params.get("ref_image")
                if not ref:
                    raise ValueError("图生图需要选择参考图")
                ref_path = _resolve_ref_path(ref)
                if not ref_path:
                    raise ValueError(f"参考图不存在: {ref}")
                import rh_zimage_api as R
                up = R.upload_image(ref_path)
                p["300"] = {"class_type": "LoadImage", "inputs": {"image": up, "upload": "image"}}
                p["301"] = {"class_type": "ImageScale", "inputs": {
                    "image": ["300", 0], "upscale_method": "lanczos",
                    "width": ["49", 0], "height": ["49", 1], "crop": "disabled"}}
                p["302"] = {"class_type": "VAEEncode", "inputs": {
                    "pixels": ["301", 0], "vae": ["57", 0]}}
                p["53"]["inputs"]["latent_image"] = ["302", 0]
                p["53"]["inputs"]["denoise"] = float(params.get("denoise", 0.65))
            with _lock:
                TASKS[task_id]["status"] = "submitting"
            pid = mod.submit(p)
            with _lock:
                TASKS[task_id]["pid"] = pid
            entry = mod.poll(pid, max_wait=900)
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
        with _lock:
            TASKS[task_id]["status"] = "error"
            TASKS[task_id]["error"] = f"{type(e).__name__}: {e}"
        _save_state()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
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
        """按扩展名推断 MIME 发一个文件（带 CORS）"""
        data = fp.read_bytes()
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
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/":
            html = HTML_FILE.read_text(encoding="utf-8") if HTML_FILE.exists() else "<h1>webui.html 缺失</h1>"
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
            self._send(200, {
                "api_key_set": bool(key), "api_key_masked": _mask(key),
                "llm": {
                    "base_url": llm["base_url"],
                    "model": llm["model"],
                    "api_key_set": bool(llm["api_key"]),
                    "api_key_masked": _mask(llm["api_key"]),
                    "style": llm["style"],
                },
            })
        elif path == "/api/backend":
            self._send(200, _backend_status())
        elif path == "/api/tasks":
            with _lock:
                t = {k: {kk: vv for kk, vv in v.items() if kk != "trace"} for k, v in TASKS.items()}
            self._send(200, {"tasks": t})
        elif path.startswith("/refs/"):
            # refs: 素材 / refs/out: 产物 均可访问
            raw = path[len("/refs/"):]
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
            name = os.path.basename(path)
            fp = OUT_DIR / name
            if not fp.exists():
                self._send(404, {"error": "not found"})
                return
            return self._send_file(fp)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        if path == "/api/market/submit":
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
                _save_config(cfg)
                self._send(200, {"ok": True})
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
                (REFS_DIR / fname).write_bytes(content)
                self._send(200, {"ok": True, "name": fname})
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
    print(f"洛宝跑图台: http://localhost:{a.port}")
    print(f"  refs: {REFS_DIR} | 输出: {OUT_DIR}")
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
