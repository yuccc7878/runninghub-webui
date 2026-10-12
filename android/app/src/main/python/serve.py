# -*- coding: utf-8 -*-
"""安卓端启动入口：把 rh_webui 的运行时根目录指到可写目录，再拉起 HTTP 服务。"""
import os
import sys
import threading
from pathlib import Path

import rh_webui


def start(root: str):
    """由 Kotlin 调用，root = app 私有可写目录（filesDir）。"""
    root = Path(root)

    # ---- 覆盖运行时路径到可写目录 ----
    rh_webui.BASE = root
    rh_webui.REFS_DIR = root / "refs"
    rh_webui.OUT_DIR = root / "workflows" / "webui_out"
    rh_webui.HTML_FILE = root / "webui.html"
    rh_webui.CONFIG_FILE = root / "workflows" / "api_key.json"

    # 确保目录存在
    (root / "refs").mkdir(parents=True, exist_ok=True)
    (root / "workflows" / "webui_out").mkdir(parents=True, exist_ok=True)

    # scripts 目录指向 chaquopy 包内路径（模块可从 sys.path 找到）
    # rh_webui.SCRIPTS_DIR 保留原始值即可，_run_task 通过 importlib 加载模块

    def _serve():
        from http.server import ThreadingHTTPServer
        # 绑 IPv4 loopback；前端 API_BASE 固定用 127.0.0.1（IPv4），保证必通
        server = ThreadingHTTPServer(("127.0.0.1", rh_webui.PORT), rh_webui.Handler)
        server.serve_forever()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    print(f"[android] 洛宝跑图台已启动: http://127.0.0.1:{rh_webui.PORT}")


if __name__ == "__main__":
    # 本地预览调试用
    start(os.environ.get("RH_ROOT", os.getcwd()))