#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RunningHub Illustrious 二次元文生图脚本
========================================
封装平台工作流 2050930923734093825「Illustrious _ Anime(WAI-illustrious-SDXL)」，
提供两个二次元增强 LoRA 的可勾选控制：
  - Smooth_Booster_v5.safetensors   （画质平滑/细节增强，推荐 0.9）
  - CunnystyleV10.safetensors       （二次元风格，推荐 0.8）

平台工作流默认还挂 Detail Slider LoRA 与 胸部尺寸滑块（Illustrious 系）。
本脚本通过 nodeInfoList 控制 LoRA 槽位：勾选=传实际值，不勾=传 "None"/0 覆盖默认。

走 /task/openapi/ai-app/run 提交 + /task/openapi/outputs 轮询下载（与市场面板同一通道）。
"""

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

API_KEY = ""
BASE = "https://www.runninghub.ai"

WORKFLOW_ID = "2050930923734093825"

# ---- 可勾选 LoRA（nodeId=6 的 Lora Loader Stack 槽位）----
LORA_SLOTS = {
    "smooth_booster": {          # 画质增强
        "slot": "01",
        "file": "Smooth_Booster_v5.safetensors",
        "default_strength": 0.9,
        "desc": "画质平滑 / 细节增强（Illustrious 系）",
    },
    "cunnystyle": {              # 二次元风格
        "slot": "03",
        "file": "CunnystyleV10.safetensors",
        "default_strength": 0.8,
        "desc": "二次元动漫风格（Illustrious 系）",
    },
}

# 默认关闭的槽位（Detail Slider 02 / 胸部滑块 04），如需保留可调
CLOSED_SLOTS = {"02": "None", "04": "None"}
# 关闭槽位的强度（0 = 不生效）
CLOSED_SLOT_STRENGTH = 0.0


def _post(path, body, timeout=30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
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


def _get_raw_input_nodes():
    """拉工作流详情，返回原始 inputNodes（含 nodeId/fieldName/fieldValue/fieldType）"""
    d = _post("/api/webapp/detail", {"webappId": WORKFLOW_ID}, timeout=20)
    if d.get("code") != 0 and d.get("ok") is not True:
        # _post 走的路径无 ok 字段，统一按 code 判断
        pass
    detail = d.get("data") or {}
    return detail.get("inputNodes") or [], detail


def _node_value(nodes, node_id, field_name, default=None):
    for n in nodes:
        if str(n.get("nodeId")) == str(node_id) and n.get("fieldName") == field_name:
            return n.get("fieldValue", default)
    return default


def build_node_info_list(
    prompt_text: str = None,
    width: int = 936,
    height: int = 1368,
    steps: int = 20,
    cfg: float = 2.0,
    use_smooth: bool = True,
    smooth_strength: float = 0.9,
    use_cunnystyle: bool = True,
    cunnystyle_strength: float = 0.8,
    nodes=None,
):
    """构造 nodeInfoList。nodes 为原始 inputNodes（不传则自动拉取）"""
    if nodes is None:
        nodes, _ = _get_raw_input_nodes()

    node_list = []

    def set_node(nid, fname, val):
        node_list.append({"nodeId": str(nid), "fieldName": fname, "fieldValue": str(val)})

    # 尺寸 / 步数 / CFG
    if _node_value(nodes, "15", "width") is not None:
        set_node("15", "width", width)
        set_node("15", "height", height)
    if _node_value(nodes, "1", "steps") is not None:
        set_node("1", "steps", steps)
        set_node("1", "cfg", cfg)

    # 正向 prompt（JjkText node 120）
    if prompt_text and _node_value(nodes, "120", "text") is not None:
        set_node("120", "text", prompt_text)

    # LoRA 槽位：勾选传实际值；关闭的槽位传 None/0 覆盖默认
    # nodeId 6 = 主 Lora Loader Stack
    for key, info in LORA_SLOTS.items():
        slot = info["slot"]
        if (key == "smooth_booster" and use_smooth) or (key == "cunnystyle" and use_cunnystyle):
            set_node("6", f"lora_{slot}", info["file"])
            st = smooth_strength if key == "smooth_booster" else cunnystyle_strength
            set_node("6", f"strength_{slot}", st)
        else:
            set_node("6", f"lora_{slot}", "None")
            set_node("6", f"strength_{slot}", CLOSED_SLOT_STRENGTH)

    # 其余槽位默认关闭（Detail Slider / 胸部滑块）
    for slot, default_val in CLOSED_SLOTS.items():
        if _node_value(nodes, "6", f"lora_{slot}") is not None:
            set_node("6", f"lora_{slot}", default_val)
            set_node("6", f"strength_{slot}", CLOSED_SLOT_STRENGTH)

    # 去重（同 nodeId+fieldName 只留最后一个）
    seen = {}
    for e in node_list:
        seen[(e["nodeId"], e["fieldName"])] = e
    return list(seen.values())


def submit(node_info_list):
    """提交任务，返回 taskId"""
    body = {
        "webappId": WORKFLOW_ID,
        "apiKey": API_KEY,
        "nodeInfoList": node_info_list,
    }
    d = _post("/task/openapi/ai-app/run", body, timeout=40)
    if d.get("code") != 0:
        raise RuntimeError(f"提交失败: {d}")
    data = d.get("data") or {}
    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"未获取到 taskId: {d}")
    return task_id


class PollTimeout(Exception):
    """轮询超时，任务可能仍远端正跑"""


def poll(task_id, max_wait=1800, interval=10):
    """轮询任务到完成/失败，返回输出文件列表"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        d = _post("/task/openapi/outputs", {"taskId": task_id, "apiKey": API_KEY}, timeout=30)
        code = d.get("code")
        if code == 0:
            return d.get("data") or []
        if code == 805:
            raise RuntimeError(f"任务失败: {json.dumps(d.get('data'), ensure_ascii=False)[:300]}")
        elif code not in (804, 813):
            # 未知错误码但非完成，继续等（保守）
            pass
        time.sleep(interval)
    raise PollTimeout(f"轮询超时 {max_wait}s: {task_id}")


def download(file_url, dest_dir="outputs"):
    """下载输出文件，返回本地路径"""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    fname = os.path.basename(file_url.split("?")[0]) or f"output_{int(time.time())}.png"
    dest = os.path.join(dest_dir, fname)
    req = urllib.request.Request(file_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


def run(
    prompt_text: str = None,
    width: int = 936,
    height: int = 1368,
    steps: int = 20,
    cfg: float = 2.0,
    use_smooth_booster: bool = True,
    smooth_strength: float = None,
    use_cunnystyle: bool = True,
    cunnystyle_strength: float = None,
    download_dir: str = "outputs",
    on_submit: callable = None,
):
    """一键：组装 → 提交 → 轮询 → 下载 → 返回 {task_id, saved}"""
    if not prompt_text:
        raise ValueError("需要 prompt_text")

    sm_st = smooth_strength if smooth_strength is not None else LORA_SLOTS["smooth_booster"]["default_strength"]
    cy_st = cunnystyle_strength if cunnystyle_strength is not None else LORA_SLOTS["cunnystyle"]["default_strength"]

    nodes, _ = _get_raw_input_nodes()
    node_list = build_node_info_list(
        prompt_text=prompt_text, width=width, height=height, steps=steps, cfg=cfg,
        use_smooth=use_smooth_booster, smooth_strength=sm_st,
        use_cunnystyle=use_cunnystyle, cunnystyle_strength=cy_st,
        nodes=nodes,
    )

    names = []
    if use_smooth_booster:
        names.append(f"Smooth_Booster_v5@{sm_st}")
    else:
        names.append("Smooth_Booster_v5(关)")
    if use_cunnystyle:
        names.append(f"CunnystyleV10@{cy_st}")
    else:
        names.append("CunnystyleV10(关)")
    print(f"[LoRA] {' + '.join(names)}")

    task_id = submit(node_list)
    print(f"[提交成功] taskId = {task_id}")
    if on_submit:
        on_submit(task_id)

    files = poll(task_id)
    print(f"[执行完成] {len(files)} 个输出")
    saved = []
    for f_ in files:
        url = f_.get("fileUrl") or f_.get("url")
        if url:
            path = download(url, download_dir)
            saved.append(path)
            print(f"  已下载: {path}")
    return {"task_id": task_id, "saved": saved}


# ==================== CLI ====================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="RunningHub Illustrious 二次元文生图")
    ap.add_argument("--prompt", type=str, required=True, help="提示词")
    ap.add_argument("--width", type=int, default=936)
    ap.add_argument("--height", type=int, default=1368)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--cfg", type=float, default=2.0)
    ap.add_argument("--smooth", type=float, default=None, help="Smooth_Booster 强度，0=关闭")
    ap.add_argument("--cunnystyle", type=float, default=None, help="Cunnystyle 强度，0=关闭")
    ap.add_argument("--out", type=str, default="outputs")
    a = ap.parse_args()

    run(
        prompt_text=a.prompt, width=a.width, height=a.height,
        steps=a.steps, cfg=a.cfg,
        use_smooth_booster=a.smooth != 0,
        smooth_strength=(a.smooth if a.smooth not in (None, 0) else None),
        use_cunnystyle=a.cunnystyle != 0,
        cunnystyle_strength=(a.cunnystyle if a.cunnystyle not in (None, 0) else None),
        download_dir=a.out,
    )