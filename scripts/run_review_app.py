#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyCharm right-click ▶ Run. Opens Streamlit local server in browser."""
import subprocess
import sys
from pathlib import Path

app = Path(__file__).with_name("review_app.py")
print("启动 MedEvidence 审核页面；浏览器访问 http://localhost:8501")
raise SystemExit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.address", "127.0.0.1", "--server.port", "8501"]))
