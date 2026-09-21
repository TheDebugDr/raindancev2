"""Tests for raindance.evasion.launch_log — per-launch JSONL verdicts.

The module is stdlib-only, so no browser or Playwright is needed.
Run:  python -m pytest tests/test_launch_log.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.evasion import launch_log
from raindance.evasion.proxy_manager import mask


def _record(**over):
    base = dict(
        task_id="task_0dc21d41",
        site="target",
        evasion_enabled=True,
        stealth="stealth applied \u00b7 verify configured browser checks passed",
        verify_summary="configured browser checks passed",
        engine="Google Chrome 140.0.7339.124",
        fingerprint="profile:default",
        exit_label="direct",
        proxy_group="residential",
        warnings=["webgl renderer mismatch"],
    )
    base.update(over)
    return launch_log.build_record(**base)


def test_build_record_has_full_schema():
    rec = _record()
    assert set(rec) == {
        "ts", "task_id", "site", "evasion_enabled", "stealth_applied",
        "verify_summary", "engine", "fingerprint", "exit", "proxy_group",
        "warnings",
    }
    # ts is a parseable UTC ISO timestamp
    parsed = datetime.fromisoformat(rec["ts"])
    assert parsed.tzinfo is not None
    assert rec["task_id"] == "task_0dc21d41"
    assert rec["site"] == "target"
    assert rec["evasion_enabled"] is True
    assert rec["stealth_applied"] is True
    assert rec["exit"] == "direct"
    assert rec["warnings"] == ["webgl renderer mismatch"]


def test_stealth_applied_is_strict():
    assert _record()["stealth_applied"] is True
    # A session that is not really patched must never read as applied.
    not_applied = _record(stealth="stealth NOT APPLIED - webdriver flag present")
    assert not_applied["stealth_applied"] is False
    # Evasion off: a stray stealth string must not flip the verdict.
    off = _record(evasion_enabled=False, stealth="stealth applied")
    assert off["stealth_applied"] is False


def test_append_writes_one_jsonl_line_per_call(tmp_path):
    path = tmp_path / "evasion_launches.jsonl"
    assert launch_log.append_launch(_record(), path=path) is True
    assert launch_log.append_launch(
        _record(task_id="task_2", stealth="stealth NOT APPLIED"), path=path) is True
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["task_id"] == "task_0dc21d41"
    assert first["stealth_applied"] is True
    assert second["task_id"] == "task_2"
    assert second["stealth_applied"] is False


def test_proxy_credentials_never_reach_disk(tmp_path):
    path = tmp_path / "evasion_launches.jsonl"
    proxy = {
        "server": "http://203.0.113.7:8080",
        "username": "supersecretuser",
        "password": "supersecretpass",
    }
    exit_label = mask(proxy)
    assert "supersecretpass" not in exit_label
    assert "supersecretuser" not in exit_label
    assert launch_log.append_launch(
        _record(exit_label=exit_label, proxy_group="residential"), path=path
    ) is True
    content = path.read_text(encoding="utf-8")
    assert "supersecretpass" not in content
    assert "supersecretuser" not in content
    rec = json.loads(content.strip())
    assert rec["exit"] == exit_label
    assert rec["proxy_group"] == "residential"


def test_rotation_trims_oldest_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_log, "MAX_LINES", 10)
    monkeypatch.setattr(launch_log, "KEEP_LINES", 4)
    path = tmp_path / "evasion_launches.jsonl"
    for i in range(10):
        assert launch_log.append_launch(
            _record(task_id=f"task_{i}"), path=path) is True
    # This 11th append trips the rotation: keep the newest 4, then append.
    assert launch_log.append_launch(_record(task_id="task_new"), path=path) is True
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    ids = [json.loads(line)["task_id"] for line in lines]
    assert ids == ["task_6", "task_7", "task_8", "task_9", "task_new"]


def test_append_creates_missing_data_dir(tmp_path):
    path = tmp_path / "data" / "evasion_launches.jsonl"
    assert not path.parent.exists()
    assert launch_log.append_launch(_record(), path=path) is True
    assert path.exists()


def test_append_never_raises(tmp_path):
    # A directory is not appendable: must return False, not raise.
    assert launch_log.append_launch(_record(), path=tmp_path) is False
