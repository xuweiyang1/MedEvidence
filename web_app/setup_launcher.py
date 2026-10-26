"""Run from the already working (xwy) environment after installing streamlit.
Stores only interpreter location; no tokens or API keys.
"""
from pathlib import Path
import importlib.util
import sys

root = Path(__file__).resolve().parents[1]
if not (root / 'scripts' / 'run_agent.py').is_file():
    raise SystemExit('ERROR: web_app must be located next to the existing scripts/ folder.')
if importlib.util.find_spec('streamlit') is None:
    raise SystemExit('ERROR: Install web dependency first: python -m pip install -r web_app/requirements-web.txt')
file = Path(__file__).resolve().parent / '.python_path.txt'
file.write_text(sys.executable + '\n', encoding='utf-8')
print('WEB LAUNCHER CONFIGURED')
print('Python:', sys.executable)
print('Ready: double-click 启动MedEvidence网页.bat')
