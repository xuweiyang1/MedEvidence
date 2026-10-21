#!/usr/bin/env python3
"""PyCharm 右键运行：MedEvidence V1b 隔离入库管理页（8503）。"""
import subprocess
import sys
from pathlib import Path

app = Path(__file__).with_name("knowledge_trial_app_v1b.py")
raise SystemExit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app),
                                  "--server.address", "127.0.0.1", "--server.port", "8503", "--server.headless", "true"]))
