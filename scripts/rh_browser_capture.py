#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机浏览器的 localStorage leveldb 中读取 RunningHub 登录态。

只读解析，不改动浏览器任何数据。支持 Chrome / Chromium / Edge / Brave。

原理：
    Chrome 把 localStorage 写成 leveldb 记录：
        <keyLen:u8> <key> <valueLen:varint> <encFlag:u8> <value>
    key 的结构为：
        "_" + <origin> + "\x00\x01" + <真实 key>
    encFlag: 0 = UTF-16LE, 1 = UTF-8/Latin-1

用法：
    python3 rh_browser_capture.py            # 打印本机读到的会话字段
    python3 rh_browser_capture.py --verbose  # 附带尝试了哪些路径
"""

import glob
import json
import os
import re
import sys

ORIGIN = "https://www.runninghub.ai"

# JWT 字面量：三段 base64url，用固定字符集判定结尾
_JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")
_JWT_HEAD = re.compile(rb"eyJ")

# 这几个字段的值是 JWT，长度字段解析失败时可退化为直接抓 JWT
_JWT_FIELDS = {"Rh-Accesstoken", "Rh-Refreshtoken"}

# 各平台常见 profile 位置。用 glob，兼容 Default / Profile 1 等。
_PROFILE_PATTERNS = [
    # 桌面平台
    # Linux
    "~/.config/google-chrome/*/Local Storage/leveldb",
    "~/.config/chromium/*/Local Storage/leveldb",
    "~/.config/microsoft-edge/*/Local Storage/leveldb",
    "~/.config/BraveSoftware/Brave-Browser/*/Local Storage/leveldb",
    "~/.config/vivaldi/*/Local Storage/leveldb",
    # macOS
    "~/Library/Application Support/Google/Chrome/*/Local Storage/leveldb",
    "~/Library/Application Support/Microsoft Edge/*/Local Storage/leveldb",
    "~/Library/Application Support/BraveSoftware/Brave-Browser/*/Local Storage/leveldb",
    # Windows（%LOCALAPPDATA% 由 expandvars 展开）
    "%LOCALAPPDATA%/Google/Chrome/User Data/*/Local Storage/leveldb",
    "%LOCALAPPDATA%/Microsoft/Edge/User Data/*/Local Storage/leveldb",
    "%LOCALAPPDATA%/BraveSoftware/Brave-Browser/User Data/*/Local Storage/leveldb",
    # ---- Android ----
    # 宿主 App 自己的 WebView（在 Termux/proot 内通常唯一可见的 WebView 数据）。
    # 用 RH_APP_ID 或环境变量兜底；下面几个通配由 _android_webview_dirs 动态补全。
    "/data/data/com.android.chrome/app_chrome/*/Local Storage/leveldb",
    "/data/user/0/com.android.chrome/app_chrome/*/Local Storage/leveldb",
    "/data/data/com.quark.browser/app_webview/*/Local Storage/leveldb",
    "/data/data/com.UCMobile/app_webview/*/Local Storage/leveldb",
    "/data/data/com.microsoft.emmx/app_chrome/*/Local Storage/leveldb",
]

# 需要取出的字段：localStorage key -> 输出字段名
_FIELD_MAP = {
    "Rh-Accesstoken": "token",
    "Rh-Refreshtoken": "refresh",
    "Rh-Identify": "identify",
    "Rh-Comfy-Auth": "comfy",
    "Rh-Comfy-Expire-In": "comfyExpire",
    "Rh-Expire-In": "expire",
}


def _read_varint(buf, pos):
    """读 leveldb 的 varint 长度。"""
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _decode_value(raw):
    """value 首字节是 encoding flag：0=UTF-16LE, 1=UTF-8/Latin-1。"""
    if not raw:
        return ""
    flag = raw[0]
    body = raw[1:]
    if flag == 0:
        try:
            return body.decode("utf-16-le", "replace")
        except Exception:
            return body.decode("latin-1", "replace")
    try:
        return body.decode("utf-8", "replace")
    except Exception:
        return body.decode("latin-1", "replace")


def _is_plausible_text(s):
    """剔除错位记录：value 必须是可打印文本。"""
    if not s:
        return False
    printable = sum(1 for c in s if c.isprintable() or c in "\r\n\t")
    return printable / len(s) >= 0.85


def _parse_with_origin_prefix(data, origin):
    """标准 Chromium 布局：完整的 "_origin\\x00\\x01" 前缀可见时按 keyLen 解析。"""
    found = {}
    origin_b = origin.encode("utf-8")
    marker = b"_" + origin_b + b"\x00\x01"
    prefix_len = len(origin_b) + 3

    idx = 0
    while True:
        idx = data.find(marker, idx)
        if idx < 0:
            break
        kstart = idx
        if kstart < 1:
            idx = kstart + 1
            continue

        key_len = data[kstart - 1]
        if key_len <= prefix_len or key_len > 250:
            idx = kstart + 1
            continue
        key_end = kstart + key_len
        if key_end >= len(data):
            break

        key = data[kstart:key_end].decode("utf-8", "replace")

        try:
            vlen, vpos = _read_varint(data, key_end)
        except Exception:
            idx = kstart + 1
            continue

        if vlen <= 0 or vlen > 5 * 1024 * 1024 or vpos >= len(data):
            idx = kstart + 1
            continue

        raw = data[vpos:vpos + vlen]
        if len(raw) < vlen:
            idx = kstart + 1
            continue

        val = _decode_value(raw)
        if val and _is_plausible_text(val):
            found[key[len(marker):]] = val
            idx = vpos + vlen
        else:
            idx = kstart + 1

    return found


def _try_jwt_after(data, key_end):
    """key 之后紧跟的 JWT（兼容无法可靠解析长度字段的 leveldb 变体）。

    MIUI 浏览器等厂商 WebView 写入的 value 长度前缀不是标准 varint
    （实测 `01 4c 85 02 00 00 00 00 01 <320字节token>`，长度字段解码失败），
    与其逆向各家私有编码，不如直接在 key 之后定位第一个 JWT 字面量。
    JWT 的字符集固定（base64url + 两个点），结尾天然可判定。
    """
    window = data[key_end:key_end + 16]
    m = _JWT_HEAD.search(window)
    if not m:
        return None
    start = key_end + m.start()
    m2 = _JWT_RE.match(data, start)
    if not m2:
        return None
    return m2.group().decode("ascii", "replace")


def _parse_leveldb_file(path, origin):
    """从单个 .ldb / .log 文件中提取 origin 下的所有键值。"""
    found = {}
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return found

    # 只认 key 名（真实 key 在 leveldb 里可能带共享前缀压缩，
    # 未必能还原出完整的 "_origin\x00\x01" 前缀，直接按 key 名定位更稳）
    for real_key in _FIELD_MAP.keys():
        marker = real_key.encode("utf-8")
        idx = 0
        while True:
            idx = data.find(marker, idx)
            if idx < 0:
                break
            key_end = idx + len(marker)

            # 方式一：key 尾紧跟标准 varint 长度 + encFlag
            val = None
            try:
                vlen, vpos = _read_varint(data, key_end)
                if 0 < vlen <= 5 * 1024 * 1024 and vpos < len(data):
                    raw = data[vpos:vpos + vlen]
                    if len(raw) >= vlen:
                        cand = _decode_value(raw)
                        if cand and _is_plausible_text(cand):
                            val = cand
            except Exception:
                val = None

            # 方式二：长度字段解析不出来时，直接抓 key 后的 JWT
            if val is None and real_key in _JWT_FIELDS:
                val = _try_jwt_after(data, key_end)

            if val:
                found[real_key] = val
                idx = key_end
            else:
                idx = key_end

    # 原逻辑：完整 origin 前缀可见时也能解析（保留给标准 Chromium 布局）
    found.update(_parse_with_origin_prefix(data, origin))
    return found


def _android_webview_dirs():
    """列出 Android 上可访问的 WebView localStorage 目录。

    为什么不能只靠 glob：在 Termux / proot 环境里 /data/data 只挂载了宿主 App
    自己（ls /data/data 只返回 com.xxx），通配符无法枚举出其他包名。
    所以这里直接扫描 /data/data/<pkg>/ 下真实存在的目录，再拼 WebView 存放路径。
    """
    out = []
    roots = ["/data/data", "/data/user/0"]
    for root in roots:
        try:
            if not os.path.isdir(root):
                continue
            pkg_dirs = os.listdir(root)
        except Exception:
            continue
        for pkg in pkg_dirs:
            for sub in ("app_webview", "app_chrome", "app_webview_sandbox"):
                base = os.path.join(root, pkg, sub)
                if not os.path.isdir(base):
                    continue
                try:
                    profiles = os.listdir(base)
                except Exception:
                    continue
                for prof in profiles:
                    d = os.path.join(base, prof, "Local Storage", "leveldb")
                    if os.path.isdir(d) and d not in out:
                        out.append(d)
    return out


def _candidate_dirs():
    """展开所有候选 leveldb 目录，按最近修改时间从新到旧排序。"""
    seen = []
    for pat in _PROFILE_PATTERNS:
        p = os.path.expandvars(os.path.expanduser(pat))
        for hit in glob.glob(p):
            if os.path.isdir(hit) and hit not in seen:
                seen.append(hit)
    for d in _android_webview_dirs():
        if d not in seen:
            seen.append(d)
    seen.sort(key=lambda d: os.path.getmtime(d), reverse=True)
    return seen


def _read_origin(ldb_dir, origin):
    """读取一个 leveldb 目录下指定 origin 的全部键值（后写覆盖先写）。"""
    files = []
    for ext in ("*.ldb", "*.log"):
        files.extend(glob.glob(os.path.join(ldb_dir, ext)))
    files.sort(key=lambda p: os.path.getmtime(p))

    merged = {}
    for f in files:
        merged.update(_parse_leveldb_file(f, origin))
    return merged


def capture(verbose=False):
    """读取本机浏览器中的 RunningHub 会话。

    返回 {'ok': True, 'token': ..., ...} 或 {'ok': False, 'error': ...}
    """
    tried = []
    for d in _candidate_dirs():
        tried.append(d)
        kv = _read_origin(d, ORIGIN)
        tok = kv.get("Rh-Accesstoken", "").strip()
        if not tok:
            continue

        out = {"ok": True, "source": d, "token": tok}
        for src_key, dst_key in _FIELD_MAP.items():
            if dst_key == "token":
                continue
            v = kv.get(src_key, "")
            if dst_key in ("comfyExpire", "expire"):
                try:
                    v = int(str(v).strip() or 0)
                except Exception:
                    v = 0
            else:
                v = (v or "").strip()
            out[dst_key] = v

        # userInfo 里常有昵称/邮箱，取一下便于展示
        try:
            ui = json.loads(kv.get("userInfo") or "{}")
            out["name"] = ui.get("userName") or ui.get("nickName") or ""
            out["email"] = ui.get("email") or ""
        except Exception:
            pass

        if verbose:
            out["tried"] = tried
        return out

    err = "未在本机浏览器中找到 RunningHub 登录状态"
    if verbose:
        return {"ok": False, "error": err, "tried": tried}
    return {"ok": False, "error": err}


def main():
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    r = capture(verbose=verbose)
    if not r.get("ok"):
        print(r["error"], file=sys.stderr)
        if r.get("tried"):
            for d in r["tried"]:
                print("  尝试: %s" % d, file=sys.stderr)
        return 2
    # 不打印完整 token
    preview = dict(r)
    if preview.get("token"):
        preview["token"] = preview["token"][:24] + "...(%d 字符)" % len(r["token"])
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
