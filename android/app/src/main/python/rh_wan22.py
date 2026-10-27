#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub · Wan Animate 2.2 动作迁移（App 通道版）
==================================================
AI 应用：2113761739660918785「wan2.2动作迁移」
画布工作流：2113394019006259202（V3）

通道说明
--------
走 App 通道（/task/openapi/ai-app/run + nodeInfoList），不是 proxy /prompt。
原因：App 通道按 webappId 调用，只提交「发布时暴露的字段」，
      节点内部结构由平台锁定，不会被改坏，也更适合对外提供稳定接口。

暴露字段（/api/webapp/detail → inputNodes，实测）
    57  image     参考图（决定身份：脸/发型/服装）
    63  video     源视频（决定动作/姿态/镜头）
    190 prompt    提示词（发布默认值可能被 App 层覆盖）
    216 int       人脸增强开关：1=开（推荐） / 0=关

分辨率与朝向
------------
画布内已改为「自动判断」，按源视频宽高比自动决定输出：
    源视频宽 > 高  → 832x480（横屏）
    否则           → 480x832（竖屏）
    其他比例（4:3 / 1:1 / 21:9）→ 归到上面两档之一，用 crop 裁切填满，
    不拉伸、不变形。
因为 width/height 没有暴露成 App 输入，所以 App 通道无法手动指定分辨率，
这是有意为之：避免用户填出 OOM 的尺寸。

技术路线：生成式角色替换
  - 参考图决定「身份」（脸/发型/服装）
  - 源视频决定「动作」（vitpose_h_wholebody 骨骼 + yolov10m 人脸）
  - ⚠️ 会连同服装/背景一起重画（区别于 ReActor 的像素级只换脸）

后处理：12fps 抽帧 → Wan 采样 → RIFE×2 补帧 → 24fps 输出，保留原音轨。
帧数按 4n+1 对齐（Wan 模型要求），会有轻微裁剪。

实测耗时（Standard 模式，10 秒输入）
    10s 竖屏 → 输出 9.75s / 234 帧 → 7 分 53 秒
    10s 横屏 → 输出 9.71s / 233 帧 → 8 分 37 秒
    约 50 秒 / 每输出秒，线性增长。
"""
import json
import os
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""   # 由 webui 运行时注入
BASE = "https://www.runninghub.ai"

# AI 应用 ID（App 通道用这个，不是工作流 ID）
APP_ID = "2113761739660918785"
# 画布工作流 ID（仅作记录，App 通道不直接用）
WORKFLOW_ID = "2113394019006259202"

# App 暴露的节点字段（来自 /api/webapp/detail）
NODE = {
    "ref_image": "57",     # LoadImage（参考图 / 身份）
    "ref_video": "63",     # VHS_LoadVideo（源视频 / 动作）
    "prompt": "190",       # CR Prompt Text
    "face_idx": "216",     # Primitive integer：0=关 / 1=开
}

# 返回码
CODE_OK = 0
CODE_QUEUE_MAXED = 421    # 队列已满，可重试
CODE_FAILED = 805         # 任务执行失败

# 通用预置提示词（留空时自动使用）
# 目标：复刻示例视频里的动作姿态与镜头，身份来自参考图
DEFAULT_PROMPT = (
    "参考图中的人物，完整复刻示例视频里人物的动作、姿态与镜头节奏；"
    "保持人物外貌、发型、服装与参考图一致；保留示例视频中的道具与手持物品。"
)


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


# ==================== 上传 ====================
def upload_media(file_path, file_type="image"):
    """上传素材到 openapi 空间，返回 fileName（可直接写进 image / video 字段）。

    file_type: "image" 或 "video"
    """
    p = Path(str(file_path))
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    suffix = p.suffix.lower()
    if suffix == ".png":
        ctype = "image/png"
    elif suffix == ".webp":
        ctype = "image/webp"
    elif suffix in (".mp4",):
        ctype = "video/mp4"
    elif suffix in (".mov",):
        ctype = "video/quicktime"
    else:
        ctype = "image/jpeg"

    data = p.read_bytes()
    boundary = uuid.uuid4().hex
    parts = []
    for name, val in (("apiKey", API_KEY), ("fileType", file_type)):
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
    d = json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
    if d.get("code") != CODE_OK:
        raise RuntimeError(f"上传失败: {json.dumps(d, ensure_ascii=False)[:300]}")
    name = (d.get("data") or {}).get("fileName")
    if not name:
        raise RuntimeError(f"上传返回异常: {str(d)[:200]}")
    return name


def upload_image(p):
    return upload_media(p, "image")


def upload_video(p):
    return upload_media(p, "video")


# ==================== App 详情 ====================
def webapp_detail(webapp_id=APP_ID):
    """拉 App 详情（暴露字段 + 默认值）。失败返回 {}"""
    d = _post("/api/webapp/detail", {"webappId": webapp_id}, timeout=30)
    if d.get("code") != CODE_OK:
        return {}
    return d.get("data") or {}


def exposed_fields(webapp_id=APP_ID):
    """返回该 App 实际暴露的 {(nodeId, fieldName)} 集合"""
    data = webapp_detail(webapp_id)
    ins = data.get("inputNodes") or []
    return {(str(n.get("nodeId")), n.get("fieldName")) for n in ins}


# ==================== 构造 nodeInfoList ====================
def build_node_info(image=None, video=None, prompt_text=None,
                    face_enhance=True, fields=None, webapp_id=APP_ID):
    """构造 nodeInfoList。

    只提交 App 真正暴露的字段——提交未暴露字段会被平台忽略或报错。
    width/height 未暴露，分辨率由画布内的自适应逻辑按源视频决定。
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

    add(NODE["ref_image"], "image", image)
    add(NODE["ref_video"], "video", video)

    # 提示词：留空则用通用预置词
    txt = (prompt_text or "").strip() or DEFAULT_PROMPT
    add(NODE["prompt"], "prompt", txt)

    # 人脸增强：1=开 / 0=关
    add(NODE["face_idx"], "int", 1 if face_enhance else 0)

    return out


# ==================== 提交 / 轮询 / 下载 ====================
def submit(node_info_list, webapp_id=APP_ID, retry=5, retry_wait=20):
    """提交任务，返回 taskId。队列满（421）自动重试。"""
    body = {"webappId": webapp_id, "apiKey": API_KEY,
            "nodeInfoList": node_info_list}
    last = None
    for i in range(retry):
        d = _post("/task/openapi/ai-app/run", body, timeout=60)
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


def poll(task_id, max_wait=3600, interval=10, verbose=True, on_ping=None):
    """轮询到完成，返回产物列表 [{fileUrl, fileType, ...}]"""
    t0 = time.time()
    last_ping = 0
    while time.time() - t0 < max_wait:
        d = _post("/task/openapi/outputs",
                  {"taskId": task_id, "apiKey": API_KEY}, timeout=40)
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

        elapsed = int(time.time() - t0)
        if on_ping and elapsed - last_ping >= 30:
            last_ping = elapsed
            try:
                on_ping(elapsed)
            except Exception:
                pass
        elif verbose and elapsed % 60 < interval:
            print(f"[轮询] {elapsed}s code={code}")
        time.sleep(interval)

    raise PollTimeout(f"轮询超时 {max_wait}s: {task_id}")


def extract_videos(items):
    """从产物列表挑出视频"""
    out = []
    for it in items or []:
        url = it.get("fileUrl") or it.get("file_url") or ""
        if not url:
            continue
        low = url.split("?")[0].lower()
        if low.endswith((".mp4", ".webm", ".mov")):
            out.append(it)
    return out


def download_video(item, dest_dir="."):
    """下载视频产物到 dest_dir，返回本地路径"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = item.get("fileUrl") or item.get("file_url")
    name = os.path.basename(url.split("?")[0]) or f"out_{int(time.time())}.mp4"
    dest = dest_dir / name
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        f.write(r.read())
    return str(dest)


# ==================== 一键跑 ====================
def run(
    ref_image_path: str,
    ref_video_path: str,
    prompt_text: str = None,
    face_enhance: bool = True,
    webapp_id: str = APP_ID,
    download_dir: str = ".",
    on_submit=None,
    on_ping=None,
    verbose: bool = True,
) -> dict:
    """完整流程：上传 → 构造 → 提交 → 轮询 → 下载

    分辨率不在这里指定：画布内按源视频宽高比自动决定（横屏 832x480 / 竖屏 480x832）。
    """
    if verbose:
        print("=" * 60)
        print(f"[Wan Animate 2.2 · App] app={webapp_id} | 人脸增强={'开' if face_enhance else '关'}")
        print("  分辨率：自动（横屏 832x480 / 竖屏 480x832）")

    # 1. 上传
    img_fn = upload_image(ref_image_path)
    if verbose:
        print(f"  参考图已上传: {img_fn}")
    vid_fn = upload_video(ref_video_path)
    if verbose:
        print(f"  源视频已上传: {vid_fn}")

    # 2. 构造 nodeInfoList
    fields = exposed_fields(webapp_id)
    if verbose:
        print(f"  暴露字段: {sorted(fields)}")
    ni = build_node_info(img_fn, vid_fn, prompt_text=prompt_text,
                         face_enhance=face_enhance,
                         fields=fields, webapp_id=webapp_id)
    if verbose:
        print(f"  提交字段数: {len(ni)}")
        for x in ni:
            v = x["fieldValue"]
            if len(str(v)) > 60:
                v = str(v)[:60] + "..."
            print(f"    [{x['nodeId']}] {x['fieldName']} = {v}")

    # 3. 提交
    tid = submit(ni, webapp_id=webapp_id)
    if verbose:
        print(f"  已提交: taskId={tid}")
    if on_submit:
        try:
            on_submit(tid)
        except Exception:
            pass

    # 4. 轮询
    items = poll(tid, verbose=verbose, on_ping=on_ping)
    videos = extract_videos(items)
    if verbose:
        print(f"  产物: {len(items)} 项，其中视频 {len(videos)} 个")

    # 5. 下载
    saved = []
    for v in videos:
        path = download_video(v, download_dir)
        saved.append(path)
        if verbose:
            print(f"    ↓ {path}")

    return {"task_id": tid, "items": items, "videos": saved}


# ==================== CLI ====================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Wan Animate 2.2 动作迁移（App 通道）")
    ap.add_argument("--image", required=True, help="参考图（身份）")
    ap.add_argument("--video", required=True, help="源视频（动作）")
    ap.add_argument("--prompt", default=None, help="提示词，留空用默认")
    ap.add_argument("--no-face", action="store_true", help="关闭人脸增强")
    ap.add_argument("--out", default="./out", help="下载目录")
    a = ap.parse_args()

    # 仓库根优先，且允许 RH_BASE 覆写；找不到则保持 API_KEY 为空（由 webui 运行时注入）
    key_file = Path(os.environ.get("RH_BASE")
                    or Path(__file__).resolve().parent.parent) / "workflows" / "api_key.json"
    if key_file.exists() and not API_KEY:
        API_KEY = json.loads(key_file.read_text()).get("api_key", "")

    r = run(a.image, a.video, prompt_text=a.prompt,
            face_enhance=not a.no_face, download_dir=a.out)
    print("\n完成:", r["videos"])