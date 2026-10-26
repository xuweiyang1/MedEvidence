"""Configuration from environment/.env.agent; never hard-code API credentials."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, "1" if default else "0").strip().lower() in ("1", "yes", "true", "on")


@dataclass(frozen=True)
class Config:
    root: Path
    api_key: str
    model: str
    judge_model: str
    api_base: str
    retriever: str
    hook: str
    qdrant_path: str
    collection: str
    bm25_db: str
    embed_model: str
    device: str
    top_k: int
    max_rounds: int
    max_evidence: int
    coverage_threshold: float
    trace_content: bool
    semantic_verify: bool
    timeout_sec: int
    log_dir: Path
    # Online abstract-level literature search (opt-in).
    online_enabled: bool
    online_sources: str
    online_max_results: int
    online_max_rounds: int
    online_timeout_sec: int
    online_cache_dir: Path
    online_cache_ttl_hours: int
    online_skip_retracted: bool
    ncbi_email: str
    ncbi_api_key: str

    @classmethod
    def from_env(cls, root: Path, *, code_root: Path | None = None) -> "Config":
        """Load .env.agent from the standalone Agent folder, then project root.

        A .env.agent placed beside the Agent code is preferred. Existing environment
        variables always win, consistent with load_env() and standard deployment.
        This preserves the user's chosen nested-folder installation layout.
        """
        local_file = code_root / ".env.agent" if code_root is not None else None
        if local_file is not None and local_file.is_file():
            load_env(local_file)
        else:
            load_env(root / ".env.agent")
        model = os.getenv("MEDEVIDENCE_MODEL", "qwen-plus")
        # Coverage/verification are short judgement tasks; a faster model here
        # cuts latency and cost without touching the evidence contract.
        judge_model = os.getenv("MEDEVIDENCE_JUDGE_MODEL", "").strip() or model
        return cls(
            root=root,
            api_key=os.getenv("DASHSCOPE_API_KEY", "") or os.getenv("QWEN_API_KEY", ""),
            model=model,
            judge_model=judge_model,
            api_base=os.getenv("MEDEVIDENCE_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            retriever=os.getenv("MEDEVIDENCE_RETRIEVER", "qdrant").lower(),
            hook=os.getenv("MEDEVIDENCE_RETRIEVER_HOOK", ""),
            qdrant_path=os.getenv("MEDEVIDENCE_QDRANT_PATH", ""),
            collection=os.getenv("MEDEVIDENCE_COLLECTION", "bge_m3_v3_2_trial_clean"),
            bm25_db=os.getenv("MEDEVIDENCE_BM25_DB", ""),
            embed_model=os.getenv("MEDEVIDENCE_EMBED_MODEL", "BAAI/bge-m3"),
            device=os.getenv("MEDEVIDENCE_DEVICE", "cpu"),
            top_k=max(1, min(40, int(os.getenv("MEDEVIDENCE_TOP_K", "8")))),
            max_rounds=max(1, min(4, int(os.getenv("MEDEVIDENCE_MAX_ROUNDS", "3")))),
            max_evidence=max(1, min(30, int(os.getenv("MEDEVIDENCE_MAX_EVIDENCE", "12")))),
            coverage_threshold=max(0.0, min(1.0, float(os.getenv("MEDEVIDENCE_COVERAGE_THRESHOLD", "1.0")))),
            trace_content=env_bool("MEDEVIDENCE_TRACE_CONTENT"),
            semantic_verify=env_bool("MEDEVIDENCE_SEMANTIC_VERIFY", True),
            timeout_sec=int(os.getenv("MEDEVIDENCE_TIMEOUT_SEC", "90")),
            log_dir=root / "outputs" / "agent_runs",
            online_enabled=env_bool("MEDEVIDENCE_ONLINE_ENABLED", False),
            online_sources=os.getenv("MEDEVIDENCE_ONLINE_SOURCES", "pubmed").strip().lower(),
            online_max_results=max(1, min(10, int(os.getenv("MEDEVIDENCE_ONLINE_MAX_RESULTS", "5")))),
            online_max_rounds=max(0, min(5, int(os.getenv("MEDEVIDENCE_ONLINE_MAX_ROUNDS", "2")))),
            online_timeout_sec=max(5, min(120, int(os.getenv("MEDEVIDENCE_ONLINE_TIMEOUT_SEC", "25")))),
            online_cache_dir=root / "data" / "online_cache",
            online_cache_ttl_hours=max(1, int(os.getenv("MEDEVIDENCE_ONLINE_CACHE_TTL_HOURS", "168"))),
            online_skip_retracted=env_bool("MEDEVIDENCE_ONLINE_SKIP_RETRACTED", True),
            ncbi_email=os.getenv("MEDEVIDENCE_CONTACT_EMAIL", "").strip(),
            ncbi_api_key=os.getenv("NCBI_API_KEY", "").strip(),
        )


def resolve_project_path(root: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else root / p
