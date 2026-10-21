"""Locate the *existing* MedEvidence repository, even when code was extracted nested."""
from __future__ import annotations
from pathlib import Path


def find_project_root(code_root: Path, explicit: str | None = None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"项目目录不存在: {root}")
        return root
    base = code_root.resolve()
    for parent in (base, *base.parents):
        scripts = parent / "scripts"
        if (scripts / "index_coordinator.py").is_file():
            return parent
        if any(scripts.glob("validate_index_version*.py")):
            return parent
    return base
