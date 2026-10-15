#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub 动作迁移 · 角色替换（Wan Animate 2.2）
================================================
工作流：Wan+Animate2.2动作迁移V3(Move)+骨骼增强版 (wan_animate_v3.json)
核心节点：WanVideoAnimateEmbeds（参考图提供身份 + 源视频骨骼动作驱动）

特点：生成式角色替换 —— 参考图决定"人"（脸/发型/服装风格），源视频决定"动作"。
注意：会连同服装/背景一起重画（区别于 ReActor 的像素级只换脸）。

输入：参考图（身份）+ 源视频（动作）
输出：默认 1 份纯产物；可选（compare=True）额外输出 1 份对比条（作者自带的流程拼接）。

关键处理：
  1. Set/Get/Reroute 双向穿透（该工作流 27 个伪节点）。
  2. widget 展开修正：seed 后跟 control_after_generate，多占一位。
  3. 启用 face_images 人脸增强（作者默认 index=0 走黑图 = 关闭）。
  4. 参考图可 2 张：主图走 ref_images（单张，规避 num_refs>1 与 pose 形状冲突），
     第二张走 ClipVision.image_2（源码预留槽位）。
  5. 分辨率走 JWInteger(183/184) 控制。
"""

import json
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

# ==================== 配置 ====================
API_KEY = ""
BASE = "https://www.runninghub.ai"
PROXY = f"{BASE}/proxy/{API_KEY}"

# 节点 ID（来自 wan_animate_v3.json）
NODE = {
    "ref_a": "57",       # LoadImage（参考图A，主身份）
    "video": "63",       # VHS_LoadVideo（源视频，动作）
    "resize_a": "64",    # ImageResizeKJv2（图A 对齐尺寸）
    "clipvis": "70",     # WanVideoClipVisionEncode（image_1/image_2）
    "embeds": "62",      # WanVideoAnimateEmbeds
    "prompt": "190",     # CR Prompt Text
    "width": "184",      # JWInteger width
    "height": "183",     # JWInteger height
    "idx": "216",        # Primitive integer（人脸增强开关）
    "out_clean": "201",  # VHS_VideoCombine（纯产物，接 Decode）
    "out_cmp": "30",     # VHS_VideoCombine（对比条，接 ImageConcatMulti）
    # 需要额外注入的第二张参考图节点
    "ref_b": "300",      # LoadImage（参考图B）
    "resize_b": "302",   # ImageResizeKJv2（图B 对齐尺寸）
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


# ==================== UI JSON → API prompt（含 Set/Get/Reroute 穿透） ====================
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
    wf = json.load(open(_find_workflow(workflow_path), encoding="utf-8"))
    nodes = {n["id"]: n for n in wf["nodes"]}
    links = wf.get("links", [])
    prompt = {}

    # 1) widget 展开（seed 后跟 control_after_generate 多占一位）
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
                        wi += 1
        prompt[str(nid)] = {"class_type": cls, "inputs": inputs}

    # 2) Set/Get 同名配对
    set_inputs = {}
    for l in links:
        if len(l) < 5:
            continue
        if nodes.get(l[3], {}).get("type") == "SetNode":
            set_inputs[l[3]] = [l[1], l[2]]
    setname2id = {}
    for nid, n in nodes.items():
        if n.get("type") == "SetNode":
            wv = n.get("widgets_values")
            if isinstance(wv, list) and wv:
                setname2id[str(wv[0])] = nid
    getname2set = {}
    for nid, n in nodes.items():
        if n.get("type") == "GetNode":
            wv = n.get("widgets_values")
            gname = str(wv[0]) if isinstance(wv, list) and wv else ""
            if gname in setname2id:
                getname2set[nid] = setname2id[gname]

    def real_source(nid, depth=0):
        if depth > 20:
            return None
        t = nodes.get(nid, {}).get("type", "")
        if t == "SetNode":
            if nid in set_inputs:
                src, sout = set_inputs[nid]
                r = real_source(src, depth + 1)
                return r if r else [src, sout]
            return None
        elif t == "GetNode":
            sid = getname2set.get(nid)
            if sid is not None:
                return real_source(sid, depth + 1)
            return None
        elif "Reroute" in t:
            for l in links:
                if len(l) >= 5 and l[3] == nid:
                    r = real_source(l[1], depth + 1)
                    return r if r else [l[1], l[2]]
            return None
        return None

    # 3) 穿透赋值
    for l in links:
        if len(l) < 5:
            continue
        lid, src, sout, dst, din = l[:5]
        stype = nodes.get(src, {}).get("type", "")
        dtype = nodes.get(dst, {}).get("type", "")
        if stype in ("SetNode", "GetNode") or "Reroute" in stype:
            real = real_source(src)
            if real is None:
                continue
            src, sout = real
        if str(dst) not in prompt or str(src) not in prompt:
            continue
        if dtype in ("SetNode", "GetNode") or "Reroute" in dtype:
            continue
        node = nodes[dst]
        if din < len(node.get("inputs", [])):
            iname = node["inputs"][din].get("name")
            if iname:
                prompt[str(dst)]["inputs"][iname] = [str(src), sout]

    # 4) 删伪节点
    for pid in [str(nid) for nid, n in nodes.items()
                if n.get("type") in ("SetNode", "GetNode") or "Reroute" in n.get("type", "")]:
        prompt.pop(pid, None)
    return prompt


def build_prompt(prompt: dict, ref_a_fn: str, video_fn: str,
                 ref_b_fn: str = None, width: int = 480, height: int = 832,
                 compare: bool = False) -> dict:
    # 1) 分辨率
    prompt[NODE["width"]]["inputs"]["value"] = width
    prompt[NODE["height"]]["inputs"]["value"] = height

    # 2) 素材
    prompt[NODE["ref_a"]]["inputs"]["image"] = ref_a_fn
    prompt[NODE["video"]]["inputs"]["video"] = video_fn

    # 3) 人脸增强（作者默认 index=0 走黑图 = 关闭）
    prompt[NODE["idx"]]["inputs"]["int"] = 1

    # 4) 参考图B（可选）→ ClipVision.image_2
    if ref_b_fn:
        prompt[NODE["ref_b"]] = {"class_type": "LoadImage", "inputs": {"image": ref_b_fn}}
        prompt[NODE["resize_b"]] = {"class_type": "ImageResizeKJv2", "inputs": {
            "image": [NODE["ref_b"], 0], "width": width, "height": height,
            "upscale_method": "lanczos", "keep_proportion": "pad_edge_pixel",
            "pad_color": "0, 0, 0", "crop_position": "top", "divisible_by": 16, "device": "cpu"}}
        prompt[NODE["clipvis"]]["inputs"]["image_1"] = [NODE["resize_a"], 0]
        prompt[NODE["clipvis"]]["inputs"]["image_2"] = [NODE["resize_b"], 0]
        prompt[NODE["clipvis"]]["inputs"]["combine_embeds"] = "average"

    # 5) 对比产物（可选）：作者自带的 ImageConcatMulti 拼接条（节点 66 → 30）
    if not compare:
        prompt.pop(NODE["out_cmp"], None)
        prompt.pop("66", None)   # ImageConcatMulti（拼接）
        prompt.pop("77", None)   # ImageConcatMulti（内层）
        prompt.pop("42", None)   # GetImageSizeAndCount（仅对比链用）
    return prompt


# ==================== 提交 / 轮询 / 下载 ====================
def submit_prompt(prompt: dict, client_id: str = "rh-wananimate") -> str:
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
    ref_a_path: str,
    video_path: str,
    workflow_path: str,
    ref_b_path: str = None,
    width: int = 480,
    height: int = 832,
    compare: bool = False,
    download_dir: str = "outputs",
    on_submit=None,
):
    """Wan Animate 动作迁移：上传 → 转换 → 提交 → 轮询 → 下载

    ref_a_path: 主参考图（身份）；ref_b_path: 可选第二参考图（另一角度）
    compare=False（默认）只输出 1 份纯产物；compare=True 额外输出对比条。
    """
    if not video_path:
        raise RuntimeError("动作迁移需要源视频")
    if not ref_a_path:
        raise RuntimeError("动作迁移需要参考图")
    ref_a_fn = upload_image(ref_a_path)
    video_fn = upload_video(video_path)
    ref_b_fn = upload_image(ref_b_path) if ref_b_path else None

    prompt = ui_to_prompt(workflow_path)
    prompt = build_prompt(prompt, ref_a_fn, video_fn, ref_b_fn=ref_b_fn,
                          width=width, height=height, compare=compare)

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
    ap = argparse.ArgumentParser(description="RunningHub Wan Animate 动作迁移")
    ap.add_argument("--ref-a", required=True, help="主参考图路径")
    ap.add_argument("--ref-b", default=None, help="第二参考图路径（可选）")
    ap.add_argument("--video", required=True, help="源视频路径")
    ap.add_argument("--workflow", default=str(Path(__file__).resolve().parent.parent / "workflows" / "wan_animate_v3.json"))
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=832)
    ap.add_argument("--compare", action="store_true", help="额外输出对比条")
    ap.add_argument("--out", default="outputs")
    a = ap.parse_args()
    run(ref_a_path=a.ref_a, ref_b_path=a.ref_b, video_path=a.video, workflow_path=a.workflow,
        width=a.width, height=a.height, compare=a.compare, download_dir=a.out)