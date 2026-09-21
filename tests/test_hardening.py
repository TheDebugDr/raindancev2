"""Regression tests for the safety fixes applied 2026-09-04.

Each test here pins a specific defect that reached the money path or could
destroy unrecoverable local state. They are cheap and need no browser.
"""
from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import pytest

from raindance.core.settings import Settings
from raindance.tasks import models as M
from raindance.tasks.monitor import MonitorService


# --------------------------------------------------------------------------- #
# config.json durability
# --------------------------------------------------------------------------- #

def test_save_is_atomic_under_concurrent_writers(tmp_path):
    """12 writers + readers: a reader must never observe a partial file.

    The monitor saves twice per task per poll and up to 8 runner threads save
    per phase transition, all rewriting the same file.
    """
    cfg = tmp_path / "config.json"
    s = Settings(cfg)
    s.data["targets"] = [{"id": f"t{i}"} for i in range(60)]
    s.save()

    problems: list[str] = []

    def writer(n: int) -> None:
        for _ in range(30):
            s.data["poll"]["interval_seconds"] = n
            s.save()

    def reader() -> None:
        for _ in range(300):
            try:
                loaded = json.loads(cfg.read_text())
            except FileNotFoundError:
                problems.append("config.json vanished mid-write")
            except json.JSONDecodeError as e:
                problems.append(f"torn read: {e}")
            else:
                if len(loaded.get("targets", [])) != 60:
                    problems.append("partial payload")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not problems, problems[:3]
    assert not list(tmp_path.glob("*.tmp")), "temp files leaked"


def test_corrupt_config_is_preserved_not_overwritten(tmp_path):
    """A truncated config must be moved aside, never silently replaced.

    config.json is gitignored and holds every product, task and proxy group.
    Falling back to DEFAULTS in silence meant the next save destroyed it.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text('{"targets": [{"id": "t1"')          # truncated mid-object

    s = Settings(cfg)

    aside = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(aside) == 1
    assert '{"targets"' in aside[0].read_text()
    assert s.data["targets"] == []                       # fell back to defaults

    s.save()                                             # must not touch the copy
    assert '{"targets"' in aside[0].read_text()


def test_non_object_config_is_quarantined(tmp_path):
    """A valid-JSON but wrong-shaped config must not merge into DEFAULTS."""
    cfg = tmp_path / "config.json"
    cfg.write_text("[]")
    s = Settings(cfg)
    assert list(tmp_path.glob("config.json.corrupt-*"))
    assert isinstance(s.data, dict) and s.data["targets"] == []


def test_quarantine_names_do_not_collide(tmp_path):
    """Two bad loads in the same second must produce two distinct copies."""
    cfg = tmp_path / "config.json"
    for _ in range(3):
        cfg.write_text("{bad")
        Settings(cfg)
    assert len(list(tmp_path.glob("config.json.corrupt-*"))) == 3


# --------------------------------------------------------------------------- #
# spend ceiling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("given,expected", [
    (1, 1), (10, 10), (11, M.MAX_QUANTITY), (500, M.MAX_QUANTITY),
    (0, 1), (-4, 1), (None, 1), ("", 1),
])
def test_quantity_is_clamped_at_the_model_layer(given, expected):
    """The gate prices ONE unit and nothing re-checks the cart total, so the
    ceiling has to hold even if a UI widget is bypassed."""
    task = M.normalize_task({"id": "t", "url": "https://x/y", "quantity": given})
    assert task["quantity"] == expected


def test_quantity_widgets_declare_the_same_ceiling():
    """Both front-door quantity inputs must carry max=, not just min=."""
    for path in ("raindance/plugins/wizard.py", "raindance/plugins/execute_tab.py"):
        src = Path(path).read_text()
        assert "max=_MAX_QTY" in src, f"{path} has no quantity ceiling"


# --------------------------------------------------------------------------- #
# monitor loop
# --------------------------------------------------------------------------- #

def _monitor(**kw):
    logs: list[tuple[str, str]] = []

    class Bus:
        def log(self, msg, level="info"):
            logs.append((level, msg))

    svc = MonitorService(
        bus=Bus(), hub=None, task_store=kw.get("store"),
        proxy_groups=None, settings=None, on_stock=lambda t: None,
    )
    return svc, logs


def test_terminal_statuses_are_not_repolled():
    """A failed or stopped task must not be re-fired by the poll loop."""
    src = Path("raindance/tasks/monitor.py").read_text()
    tree = ast.parse(src)
    busy = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "busy" for t in node.targets)):
            busy = {e.value for e in node.value.elts}
    assert busy is not None, "busy set not found"
    assert {"failed", "stopped"} <= busy
    assert "idle" not in busy, "idle is the resting state and must stay pollable"


def test_one_failing_poll_does_not_kill_the_loop():
    """A raising poll must be logged and backed off, not propagate."""
    class Store:
        def __init__(self):
            self.runtime = []

        def list(self, enabled_only=False):
            return [{"id": "a", "monitor": True}, {"id": "b", "monitor": True}]

        def set_runtime(self, tid, **kw):
            self.runtime.append((tid, kw))

    store = Store()
    svc, logs = _monitor(store=store)

    attempted: list[str] = []

    def fake_poll(t, *, base, fast, jitter):
        attempted.append(t["id"])
        if t["id"] == "a":
            raise OSError("disk gone")
        return 1.0

    svc._poll_task = fake_poll

    # Drive one pass of the real loop body, then stop it.
    def stop_soon():
        svc._stop.set()
    threading.Timer(0.35, stop_soon).start()
    svc.settings = type("S", (), {"data": {}})()
    svc._loop()

    assert any(lvl == "err" and "poll failed" in m for lvl, m in logs), logs
    # The healthy task was still reached despite the failing one — this is the
    # whole point: before the guard, "a" raising ended the loop and "b" was
    # never polled again while the UI still reported "watching".
    assert "b" in attempted, attempted
    # and the failing task was backed off rather than hot-looping
    assert any(kw.get("status") == "monitoring" and "poll error" in kw.get("message", "")
               for tid, kw in store.runtime if tid == "a")


# --------------------------------------------------------------------------- #
# network exposure
# --------------------------------------------------------------------------- #

def test_web_mode_binds_loopback_by_default():
    """NiceGUI resolves host=None to 0.0.0.0 when native=False, which exposed an
    unauthenticated control plane on every interface."""
    src = Path("app.py").read_text()
    assert 'host = os.environ.get("RAINDANCE_HOST") or "127.0.0.1"' in src
    assert "port=port, host=host" in src


def test_terminal_siblings_are_not_auto_activated():
    """A sibling's stock hit must not resurrect a FAILED or STOPPED task.

    Closing this alongside the monitor busy-set matters: the monitor no longer
    re-polls a failed task, but the orchestrator could still sweep it back in
    every time another profile on the same URL saw stock.
    """
    src = Path("raindance/tasks/orchestrator.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ListComp):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if "STATUS_MONITORING" in seg and "enabled_only=True" in seg:
            assert "STATUS_FAILED" not in seg, "failed tasks are auto-reactivated"
            assert "STATUS_STOPPED" not in seg, "stopped tasks are auto-reactivated"
            assert "STATUS_ARMED" in seg, "armed tasks must stay activatable"
            return
    pytest.fail("sibling-activation comprehension not found")
