"""Compatibility entry point for the strict coverage probe.

Copy this file together with ``medevidence_coverage_probe_strict.py`` into the
original MedEvidence project's ``scripts/`` directory to preserve the old
command name.
"""

from medevidence_coverage_probe_strict import main


if __name__ == "__main__":
    raise SystemExit(main())
