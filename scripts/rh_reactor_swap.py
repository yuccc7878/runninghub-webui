#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub 视频换脸（ReActor / InsightFace）
==========================================
工作流：视频换脸工作流【AI小王子】(faceswap_aiwz.json)
核心节点：ReActorFaceSwap（inswapper_128.onnx + GFPGANv1.4.pth 人脸增强）

特点：像素级只换脸 —— 身体/服装/背景/动作完全不动（区别于 Wan Animate 的生成式换人）。

输入：参考人脸图 + 目标视频
输出：默认 1 份纯换脸 mp4；可选（compare=True）额外输出 1 份对比图（左换脸 / 右原图）。

关键改动 vs 作者原版：
  1. 删除 ImageResizeKJ(14) —— 作者写死 1024×512 横屏框，会把竖屏源压成 192×512；
     ReActor 像素级换脸无需固定尺寸，直接吃原始帧 → 保留原始分辨率。
  2. 删除 RIFE VFI(26) 插帧 —— 保持原帧率，避免时长/帧数膨胀。
  3. select_every_nth=1 保留全部帧。
  4. compare=False 时删除对比拼接链（ImageConcanate 20 / VideoCombine 21），只出一份。
"""

import json
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""   # 由 webui 运行时注入
BASE = "https://www.runninghub.ai"
PROXY = f"{BASE}/proxy/{API_KEY}"

# 节点 ID（来自 faceswap_aiwz.json）
NODE = {
    "face": "10",       # LoadImage（参考人脸）
    "video": "11",      # VHS_LoadVideo（目标视频）
    "crval": "13",      # CR Value（select_every_nth）
    "resize": "14",     # ImageResizeKJ（压扁元凶，删除）
    "reactor": "15",    # ReActorFaceSwap
    "out_clean": "18",  # VHS_VideoCombine（纯换脸产物）
    "concat": "20",     # ImageConcanate（对比拼接）
    "out_cmp": "21",    # VHS_VideoCombine（对比产物）
    "rife": "26",       # RIFE VFI（插帧，删除）
}


# ==================== 基础请求 ====================
def _request(url, method="GET", body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    if body is not None:
        req.add_header("content-type", "application/json")
    req.add_header("Authorization", API_KEY)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def _upload(path, ct, field="image"):
    data = Path(path).read_bytes()
    bd = uuid.uuid4().hex
    body = b""
    body += f"--{bd}\r\n".encode()
    body += f'Content-Disposition: form-data; name="{field}"; filename="{Path(path).name}"\r\n'.encode()
    body += f"Content-Type: {ct}\r\n\r\n".encode()
    body += data + b"\r\n"
    body += f"--{bd}--\r\n".encode()
    req = urllib.request.Request(f"{PROXY}/upload/image", data=body, method="POST")
    req.add_header("accept", "application/json")
    req.add_header("content-type", f"multipart/form-data; boundary={bd}")
    req.add_header("Authorization", API_KEY)
    return json.loads(urllib.request.urlopen(req, timeout=180).read().decode())["name"]


def upload_image(p):
    ct = "image/png" if str(p).lower().endswith(".png") else "image/jpeg"
    return _upload(p, ct)


def upload_video(p):
    return _upload(p, "video/mp4")


# ==================== UI JSON → API prompt ====================
def _find_workflow(path_or_name: str) -> str:
    """定位工作流 JSON：支持绝对路径 / 相对名，多路兜底（本地 workflows、模块同目录、模块/workflows）"""
    p = Path(path_or_name)
    if p.is_file():
        return str(p)
    name = p.name
    here = Path(__file__).resolve().parent
    for cand in (here / "workflows" / name, here / name,
                 here.parent / "workflows" / name):
        if cand.is_file():
            return str(cand)
    raise FileNotFoundError(f"找不到工作流 JSON: {path_or_name}")


def ui_to_prompt(workflow_path) -> dict:
    """把 ComfyUI UI 格式 JSON 转成 API prompt（含 widget 展开 + 连线赋值）"""
    wf = json.load(open(_find_workflow(workflow_path), encoding="utf-8"))
    nodes = {n["id"]: n for n in wf["nodes"]}
    links = wf.get("links", [])
    prompt = {}
    for nid, node in nodes.items():
        cls = node.get("type")
        if not cls or "Note" in cls:
            continue
        inputs = {}
        wv = node.get("widgets_values")
        defined = node.get("inputs", [])
        if isinstance(wv, dict):
            for inp in defined:
                nm = inp.get("name")
                if inp.get("widget") is not None and inp.get("link") is None:
                    if nm in wv and nm not in ("upload", "videopreview", "choose video to upload"):
                        inputs[nm] = wv[nm]
        elif isinstance(wv, list) and defined:
            wi = 0
            for inp in defined:
                if inp.get("widget") is not None:
                    if wi < len(wv) and inp.get("link") is None:
                        if inp.get("type") not in ("IMAGEUPLOAD", "VIDEOUPLOAD", "AUDIOUPLOAD"):
                            inputs[inp["name"]] = wv[wi]
                    wi += 1
                    if inp.get("name") in ("seed", "noise_seed"):
                        wi += 1   # seed 后跟 control_after_generate
        prompt[str(nid)] = {"class_type": cls, "inputs": inputs}
    for l in links:
        if len(l) < 5:
            continue
        lid, src, sout, dst, din = l[:5]
        if str(dst) not in prompt or str(src) not in prompt:
            continue
        node = nodes[dst]
        if din < len(node.get("inputs", [])):
            iname = node["inputs"][din].get("name")
            if iname:
                prompt[str(dst)]["inputs"][iname] = [str(src), sout]
    return prompt


def build_prompt(prompt: dict, face_fn: str, video_fn: str,
                 compare: bool = False, face_restore: float = 0.4) -> dict:
    # 1) 删压扁用的 ImageResizeKJ + RIFE 插帧
    prompt.pop(NODE["resize"], None)
    prompt.pop(NODE["rife"], None)

    # 2) ReActor 直接吃原始帧（保留原始分辨率）
    prompt[NODE["reactor"]]["inputs"]["input_image"] = [NODE["video"], 0]
    # 人脸修复强度
    prompt[NODE["reactor"]]["inputs"]["face_restore_visibility"] = face_restore

    # 3) 保留全部帧
    prompt[NODE["crval"]]["inputs"]["value"] = 1
    prompt[NODE["video"]]["inputs"]["frame_load_cap"] = 0

    # 4) 素材替换
    prompt[NODE["face"]]["inputs"]["image"] = face_fn
    prompt[NODE["video"]]["inputs"]["video"] = video_fn

    # 5) 干净产物：出片18 直接接 ReActor(15)
    prompt[NODE["out_clean"]]["inputs"]["images"] = [NODE["reactor"], 0]

    # 6) 对比产物（可选）：保留拼接链
    if compare:
        prompt[NODE["concat"]]["inputs"]["image2"] = [NODE["video"], 0]  # 右图=原图
        prompt[NODE["out_cmp"]]["inputs"]["images"] = [NODE["concat"], 0]
    else:
        prompt.pop(NODE["concat"], None)
        prompt.pop(NODE["out_cmp"], None)
    return prompt


# ==================== 提交 / 轮询 / 下载 ====================
def submit_prompt(prompt: dict, client_id: str = "rh-reactor") -> str:
    d = _request(f"{PROXY}/prompt", method="POST",
                 body={"prompt": prompt, "client_id": client_id}, timeout=120)
    if "prompt_id" in d:
        return d["prompt_id"]
    if d == {}:
        q = _request(f"{PROXY}/queue", timeout=30)
        if q.get("queue_running"):
            return str(q["queue_running"][-1][0])
        if q.get("queue_pending"):
            return str(q["queue_pending"][0][0])
    raise RuntimeError(f"submit failed: {d}")


class PollTimeout(Exception):
    pass


def poll_history(prompt_id: str, interval: int = 15, max_wait: int = 1800) -> dict:
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            d = _request(f"{PROXY}/history/{prompt_id}", timeout=30)
            if prompt_id in d:
                entry = d[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "success":
                    return entry
                for m in status.get("messages", []):
                    if m[0] == "execution_error":
                        raise RuntimeError(f"execution error: {json.dumps(m[1], ensure_ascii=False)}")
            time.sleep(interval)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                time.sleep(interval)
                continue
            raise
    raise PollTimeout(f"poll timeout after {max_wait}s: {prompt_id}")


def extract_outputs(entry: dict) -> list:
    files = []
    for nid, out in entry.get("outputs", {}).items():
        for key in ("gifs", "videos", "images"):
            for f in out.get(key, []):
                files.append({"node": nid, "filename": f.get("filename"),
                              "objectKey": f.get("objectKey"), "type": f.get("type")})
    return files


def download_output(output: dict, dest_dir: str = ".") -> str:
    url = f"https://rh-hk-images.xiaoyaoyou.com/{output['objectKey']}"
    dest = Path(dest_dir) / output["filename"]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())
    return str(dest)


# ==================== 一站式入口 ====================
def run(
    face_path: str,
    video_path: str,
    workflow_path: str,
    compare: bool = False,
    face_restore: float = 0.4,
    download_dir: str = "outputs",
    on_submit=None,
):
    """ReActor 视频换脸：上传 → 转换 → 提交 → 轮询 → 下载

    compare=False（默认）只输出 1 份纯换脸 mp4；
    compare=True 额外输出 1 份对比图（左换脸 / 右原图）。
    """
    if not video_path:
        raise RuntimeError("视频换脸需要目标视频")
    if not face_path:
        raise RuntimeError("视频换脸需要参考人脸图")
    face_fn = upload_image(face_path)
    video_fn = upload_video(video_path)

    prompt = ui_to_prompt(workflow_path)
    prompt = build_prompt(prompt, face_fn, video_fn, compare=compare, face_restore=face_restore)

    pid = submit_prompt(prompt)
    print(f"[提交成功] prompt_id = {pid}")
    if on_submit:
        on_submit(pid)

    entry = poll_history(pid)
    print("[执行完成] status = success")

    outputs = extract_outputs(entry)
    saved = []
    for out in outputs:
        fn = out.get("filename") or ""
        if fn.endswith((".mp4", ".webm", ".mov")):
            p = download_output(out, download_dir)
            saved.append(p)
            print(f"  [视频] 已下载: {p}")
    return {"prompt_id": pid, "outputs": outputs, "videos": saved, "saved": saved}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="RunningHub ReActor 视频换脸")
    ap.add_argument("--face", required=True, help="参考人脸图路径")
    ap.add_argument("--video", required=True, help="目标视频路径")
    ap.add_argument("--workflow", default=str(Path(__file__).resolve().parent.parent / "workflows" / "faceswap_aiwz.json"))
    ap.add_argument("--compare", action="store_true", help="额外输出对比图")
    ap.add_argument("--face-restore", type=float, default=0.4)
    ap.add_argument("--out", default="outputs")
    a = ap.parse_args()
    run(face_path=a.face, video_path=a.video, workflow_path=a.workflow,
        compare=a.compare, face_restore=a.face_restore, download_dir=a.out)