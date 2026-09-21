"""Per-launch evasion verdicts, persisted as JSONL.

The factory's ``last_*`` attributes and the event bus are both live-only, so
what any past launch actually reported (stealth applied? which exit? what did
verification say?) was unrecoverable once the app closed. This module appends
ONE JSON line per browser launch to ``data/evasion_launches.jsonl``:

    {"ts": ..., "task_id": ..., "site": ..., "evasion_enabled": ...,
     "stealth_applied": ..., "verify_summary": ..., "engine": ...,
     "fingerprint": ..., "exit": ..., "proxy_group": ..., "warnings": [...]}

``"exit"`` is ``"direct"`` when no proxy was used, otherwise the proxy's
*masked* identity (``host:port (use:\u2022\u2022\u2022\u2022)`` via
``proxy_manager.mask``) — credentials never reach disk.

Stdlib-only on purpose: no Playwright, no app imports, so the record path can
never break a launch and the module is trivially unit-testable. Every public
function here is best-effort and never raises.

Rotation: once the file reaches ``MAX_LINES`` lines, the oldest entries are
dropped down to ``KEEP_LINES`` before the next append. Appends use O_APPEND,
which is atomic for single small writes on POSIX; the rotate-then-append
sequence can theoretically interleave with a concurrent launch, but the worst
case is a duplicated or dropped verdict line, never a corrupted file or a
failed launch.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG_FILENAME = "evasion_launches.jsonl"
MAX_LINES = 10_000   # rotate when the file reaches this many lines
KEEP_LINES = 5_000   # lines kept by a rotation


def _repo_root() -> Path:
    """Repo root: this file lives at <root>/raindance/evasion/launch_log.py."""
    return Path(__file__).resolve().parents[2]


def log_path() -> Path:
    """Where launch verdicts live: <root>/data/evasion_launches.jsonl."""
    return _repo_root() / "data" / LOG_FILENAME


def build_record(
    *,
    task_id: Optional[str],
    site: Optional[str],
    evasion_enabled: bool,
    stealth: str,
    verify_summary: str,
    engine: str,
    fingerprint: str,
    exit_label: str,
    proxy_group: Optional[str],
    warnings: Optional[List[str]],
) -> Dict[str, Any]:
    """One launch's verdict as a JSON-serializable dict.

    ``stealth_applied`` is what matters downstream: evasion was on AND the
    session actually got patched (no "NOT APPLIED" marker).
    """
    stealth = stealth or ""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "site": site,
        "evasion_enabled": bool(evasion_enabled),
        "stealth_applied": bool(
            evasion_enabled and stealth and "NOT APPLIED" not in stealth
        ),
        "verify_summary": verify_summary or "",
        "engine": engine or "",
        "fingerprint": fingerprint or "",
        "exit": exit_label or "direct",
        "proxy_group": proxy_group,
        "warnings": [str(w) for w in (warnings or [])],
    }


def _rotate_if_needed(path: Path) -> None:
    """Trim the file to KEEP_LINES once it reaches MAX_LINES. Never raises."""
    try:
        if not path.exists():
            return
        count = 0
        with path.open("rb") as fh:
            for _ in fh:
                count += 1
                if count >= MAX_LINES:
                    break
        if count < MAX_LINES:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[-KEEP_LINES:]) + "\n",
                        encoding="utf-8")
    except Exception:  # noqa: BLE001 - rotation is advisory, never fatal
        pass


def append_launch(record: Dict[str, Any], path: Optional[Path] = None) -> bool:
    """Append one verdict line. Creates ``data/`` if missing.

    Returns True on success, False on any failure — never raises, so a logging
    problem can never break a browser launch. ``path`` is an override for
    tests; callers always use the default.
    """
    try:
        target = Path(path) if path is not None else log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001 - see docstring
        return False
