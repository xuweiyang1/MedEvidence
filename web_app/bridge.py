"""Safe subprocess adapter for the *existing* MedEvidence Agent SDK V0.3 CLI.

No RAG/Agent code is patched and no cloud keys are sent to the frontend.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent
CODE_ROOT = APP_ROOT.parent
_AGENT_LOCK = threading.Lock()
PMCID_RE = re.compile(r"^PMC[0-9]{1,12}$", re.IGNORECASE)


class AgentFrontendError(RuntimeError):
    """Expected operational failure safe to show to local users."""


def agent_entrypoint(root: Path = CODE_ROOT) -> Path:
    return root / "scripts" / "run_agent.py"


def configuration_status(root: Path = CODE_ROOT) -> dict[str, Any]:
    """Read-only coarse status. Never load or return a credential."""
    files = [root / ".env.agent", root.parent / ".env.agent"]
    config_file = next((p for p in files if p.is_file()), None)
    key_present = bool(os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY"))
    model = os.getenv("MEDEVIDENCE_MODEL")
    if config_file:
        for raw in config_file.read_text(encoding="utf-8-sig").splitlines():
            s = raw.strip()
            if s.startswith("#") or "=" not in s:
                continue
            key, value = (x.strip() for x in s.split("=", 1))
            value = value.strip("\"'")
            if key in ("DASHSCOPE_API_KEY", "QWEN_API_KEY") and value and value != "your_key_here":
                key_present = True
            if key == "MEDEVIDENCE_MODEL" and not model:
                model = value
    return {
        "code_ready": agent_entrypoint(root).is_file(),
        "key_configured": key_present,
        "model": model or "qwen-plus",
        "config_found": config_file is not None,
    }


def _extract_agent_json(text: str) -> dict[str, Any]:
    """Find CLI result even if progress/warnings precede the JSON on stdout."""
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text).lstrip("\ufeff")
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"(?m)^\s*\{", text):
        pos = text.find("{", match.start(), match.end())
        if pos < 0:
            continue
        try:
            item, _ = decoder.raw_decode(text[pos:])
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and {"answer", "status", "evidence"}.issubset(item):
            candidates.append(item)
    if candidates:
        return candidates[-1]
    raise AgentFrontendError("Agent 已运行，但未返回可解析的结构化结果。请先在命令行检查原始 Agent 是否能使用 --json。")


def run_question(question: str, *, root: Path = CODE_ROOT, timeout: int = 480) -> dict[str, Any]:
    question = question.strip()
    if not question or len(question) > 1800:
        raise AgentFrontendError("问题不能为空，且长度不能超过 1800 个字符。")
    script = agent_entrypoint(root)
    if not script.is_file():
        raise AgentFrontendError("找不到 scripts/run_agent.py。请将网页包解压在 MedEvidence_Agent_V0_3_0 文件夹中。")
    if not _AGENT_LOCK.acquire(blocking=False):
        raise AgentFrontendError("当前正在处理另一条问题。为避免本地 Qdrant 数据库并发锁冲突，请稍后再提交。")
    try:
        # The original V2 spawned a fresh Python process for EVERY question,
        # which reloads BGE-M3 on every request. In the local deployment the
        # one-worker queue can safely keep it resident instead. Pass
        # MEDEVIDENCE_WEB_ENGINE=cli to revert without touching Agent files.
        engine = os.getenv("MEDEVIDENCE_WEB_ENGINE", "resident").strip().lower()
        if engine == "resident" and Path(root).resolve() == CODE_ROOT.resolve():
            try:
                from resident_runner import run_resident_question
                return run_resident_question(question)
            except Exception as exc:
                raise AgentFrontendError(
                    "常驻 Agent 执行失败（" + type(exc).__name__ +
                    "）。可设置 MEDEVIDENCE_WEB_ENGINE=cli 回退到原版启动方式。"
                ) from exc
        child_env = os.environ.copy()
        child_env.update({
            "USE_TF": "0", "USE_FLAX": "0", "USE_TORCH": "1",
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
            "MEDEVIDENCE_TRACE_CONTENT": "0",
        })
        try:
            process = subprocess.run(
                [sys.executable, str(script), "--question", question, "--json"],
                cwd=str(root), env=child_env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentFrontendError("查询超时。首次运行可能需要加载嵌入模型，请检查本地索引和网络连接。") from exc
        except OSError as exc:
            raise AgentFrontendError(f"无法启动 Agent 子进程（{type(exc).__name__}）。") from exc
        if process.returncode != 0:
            # stderr may contain question text, paths, or credentials: do not surface it.
            raise AgentFrontendError(
                f"Agent 未成功完成（退出码 {process.returncode}）。请在 (xwy) 环境里先执行原有 scripts/run_agent.py 排查环境。"
            )
        return _extract_agent_json(process.stdout)
    finally:
        _AGENT_LOCK.release()


def pmc_url(pmcid: str) -> str | None:
    if not isinstance(pmcid, str) or not PMCID_RE.fullmatch(pmcid.strip()):
        return None
    return "https://pmc.ncbi.nlm.nih.gov/articles/" + pmcid.strip().upper() + "/"


def render_markdown_report(question: str, result: dict[str, Any]) -> str:
    """Text export excludes credentials, local trace path, and hidden evaluator inputs."""
    lines = [
        "# MedEvidence 循证问答报告", "",
        "## 问题", question.strip(), "", "## 回答", str(result.get("answer", "")), "",
        "## 检索结果概览",
        f"- 状态：{result.get('status', 'unknown')}",
        f"- 检索次数：{result.get('rounds', 'unknown')}",
        f"- 启发式证据覆盖率：{result.get('coverage_ratio', 0)}", "",
        "## 证据来源",
    ]
    for e in result.get("evidence", []):
        label = f"[{e.get('id', '?')}] {e.get('title') or '未标注标题'}"
        url = pmc_url(e.get("pmcid") or "")
        lines.append(f"- {label}" + (f" — {url}" if url else ""))
    lines.extend(["", "## 研究限制"])
    lines.extend("- " + str(v) for v in result.get("limitations", []))
    lines.extend(["", "---", "仅供科研与教学参考，不构成临床诊疗建议。"])
    return "\n".join(lines)
