#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launch Streamlit from PyCharm ▶ Run using the active Python interpreter."""
from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    project = Path(__file__).resolve().parents[1]
    app = project / "scripts" / "knowledge_manager_app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app),
                    "--server.address", "127.0.0.1", "--server.port", "8502"],
                   cwd=str(project), check=True)
