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

    # ---- 清理 sys.path 中的幽灵路径 ----
    # rh_webui 在模块级可能插入 AssetFinder/scripts（Chaquopy 下该目录不存在）。
    # 只要 sys.path 里存在不存在的目录，Chaquopy importer 在 import urllib.request
    # （→http.client→socket→_socket C 扩展）时就会抛 FileNotFoundError，
    # 导致点数/市场/跑图/润色等所有外网请求失败。这里做一次兜底清除。
    _bad = str(Path(rh_webui.__file__).resolve().parent.parent / "scripts")
    while _bad in sys.path:
        sys.path.remove(_bad)

    # ---- 覆盖运行时路径到可写目录 ----
    rh_webui.BASE = root
    rh_webui.REFS_DIR = root / "refs"
    rh_webui.OUT_DIR = root / "workflows" / "webui_out"
    rh_webui.HTML_FILE = root / "webui.html"
    rh_webui.CONFIG_FILE = root / "workflows" / "api_key.json"

    # 确保目录存在
    (root / "refs").mkdir(parents=True, exist_ok=True)
    (root / "workflows" / "webui_out").mkdir(parents=True, exist_ok=True)

    # scripts 目录：Chaquopy 下脚本模块与 rh_webui.py 同目录（AssetFinder/app），
    # 已由 Chaquopy 自动加入 sys.path，无需再手动插入
    rh_webui.SCRIPTS_DIR = Path(rh_webui.__file__).resolve().parent

    def _serve():
        from http.server import ThreadingHTTPServer
        try:
            # 绑 IPv4 loopback；前端 API_BASE 固定用 127.0.0.1（IPv4），保证必通
            server = ThreadingHTTPServer(("127.0.0.1", rh_webui.PORT), rh_webui.Handler)
            server.serve_forever()
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[android] 后端启动失败: {e!r}")

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    print(f"[android] 洛宝跑图台已启动: http://127.0.0.1:{rh_webui.PORT}")


if __name__ == "__main__":
    # 本地预览调试用
    start(os.environ.get("RH_ROOT", os.getcwd()))