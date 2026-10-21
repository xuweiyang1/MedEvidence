#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机 HTTPS 连通性诊断：判断默认证书链是否可用，以及可选的修复手段。"""
from __future__ import annotations

import ssl
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TEST_URL = "https://eutils.ncbi.nlm.nih.gov/eutils/einfo.fcgi?retmode=json"


def try_fetch(label: str, context=None) -> None:
    try:
        with urllib.request.urlopen(TEST_URL, timeout=30, context=context) as resp:
            data = resp.read()
        print(f"[OK]   {label}（{len(data)} B）")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label}: {exc}")


def main() -> int:
    print("默认证书路径:", ssl.get_default_verify_paths())
    try:
        import certifi
        print("certifi:", certifi.where())
    except ImportError:
        print("certifi: 未安装")
    try:
        import truststore
        print("truststore: 已安装")
    except ImportError:
        print("truststore: 未安装")

    ctx_default = ssl.create_default_context()
    try_fetch("默认上下文", ctx_default)

    try:
        import certifi
        ctx_certifi = ssl.create_default_context(cafile=certifi.where())
        try_fetch("certifi 上下文", ctx_certifi)
    except ImportError:
        pass

    try:
        import truststore
        ctx_ts = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try_fetch("truststore 上下文", ctx_ts)
    except ImportError:
        pass

    ctx_unverified = ssl._create_unverified_context()
    try_fetch("跳过验证（兜底）", ctx_unverified)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
