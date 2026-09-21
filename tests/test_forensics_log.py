"""Failure forensics — the append-only JSONL failure log.

Covers: proxy masking (no credentials ever hit disk), wall-match detection
(which challenge text fired), one-record-per-denial appends, and the
runner's _deny funnel writing a real record end to end.

Everything here is observation-only: no retry, gate, or CAPTCHA behavior is
touched by the code under test.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.core import classify as C
from raindance.core import forensics as F
from raindance.tasks import runner as runner_mod


# --------------------------------------------------------------------------- #
# stubs (same shape as test_runner_money_path.py)
# --------------------------------------------------------------------------- #
class Bus:
    def __init__(self):
        self.lines = []

    def log(self, msg, level="info"):
        self.lines.append((level, str(msg)))


class Store:
    def __init__(self):
        self.runtime = {}

    def set_runtime(self, tid, **kw):
        self.runtime[tid] = kw

    def get(self, tid):
        return {"id": tid}


class Settings:
    def __init__(self, path):
        self.data = {"forensics_path": path}


class Page:
    def __init__(self, url="https://www.target.com/p/x/1",
                 title="Robot or Human?"):
        self.url = url
        self._title = title

    def title(self):
        return self._title

    def screenshot(self, **kw):
        return b""


def _runner(dest):
    return runner_mod.TaskRunner(
        bus=Bus(), hub=None, task_store=Store(), profile_store=None,
        proxy_groups=None, captcha=None, settings=Settings(str(dest)),
        should_stop=lambda: False, on_status=None,
    )


def _read_records(dest):
    return [json.loads(line) for line in
            Path(dest).read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# proxy masking
# --------------------------------------------------------------------------- #
def test_mask_proxy_direct():
    assert F.mask_proxy(None) == "direct"
    assert F.mask_proxy({}) == "direct"
    assert F.mask_proxy("") == "direct"
    assert F.mask_proxy({"server": ""}) == "direct"


def test_mask_proxy_masks_credentials():
    out = F.mask_proxy(
        {"server": "http://user:s3cretpw@proxy.example.com:8080"})
    assert "s3cretpw" not in out
    assert "user" not in out
    assert "proxy.example.com:8080" in out
    assert out.startswith("http://***@")


def test_mask_proxy_bare_string_and_no_credentials():
    assert F.mask_proxy("http://u:pw@h:1") == "http://***@h:1"
    # no credentials present — nothing to hide, host passes through
    assert F.mask_proxy({"server": "http://proxy.example.com:8080"}) == \
        "http://proxy.example.com:8080"


# --------------------------------------------------------------------------- #
# wall_match
# --------------------------------------------------------------------------- #
def test_wall_match_reports_fired_text():
    assert C.wall_match("<html><body>Access Denied — perimeterx</body></html>")
    m = C.wall_match("please verify you are human to continue")
    assert m is not None and "human" in m.lower()
    assert C.is_bot_wall("please verify you are human to continue") is True


def test_wall_match_ignores_dormant_boilerplate():
    # A <style> rule naming px-captcha ships on every Target PDP whether or
    # not a challenge is showing — it must not count as a wall.
    html = ("<html><head><style>#px-captcha-modal{display:none}</style>"
            "<script>var px='perimeterx config';</script></head>"
            "<body>a normal product page</body></html>")
    assert C.wall_match(html) is None
    assert C.is_bot_wall(html) is False


def test_wall_match_none_for_clean_page():
    assert C.wall_match("<html><body>a normal product page</body></html>") is None


# --------------------------------------------------------------------------- #
# log_failure
# --------------------------------------------------------------------------- #
def test_log_failure_appends_one_jsonl_record(tmp_path):
    dest = tmp_path / "failure_forensics.jsonl"
    ok = F.log_failure(
        path=str(dest), kind="bot_wall", task_id="t1", site="target",
        url="https://www.target.com/p/x/1", title="Robot or Human?",
        classification="blocked", detector="robot or human",
        attempt=2, action="aborted_before_add_to_cart",
        outcome="denied:blocked",
        screenshot="data/screenshots/20260921-134012-t1-denied.png",
        evasion=F.evasion_snapshot(
            evasion_on=True, proxy={"server": "http://u:pw@h:1"},
            fingerprint="fp_9f31", headless=False),
    )
    assert ok is True
    recs = _read_records(dest)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "bot_wall"
    assert rec["task_id"] == "t1"
    assert rec["site"] == "target"
    assert rec["url"] == "https://www.target.com/p/x/1"
    assert rec["page_title"] == "Robot or Human?"
    assert rec["classification"] == "blocked"
    assert rec["detector"] == "robot or human"
    assert rec["attempt"] == 2
    assert rec["action"] == "aborted_before_add_to_cart"
    assert rec["outcome"] == "denied:blocked"
    assert rec["screenshot"].endswith("-t1-denied.png")
    assert "T" in rec["timestamp"]  # UTC ISO
    ev = rec["evasion"]
    assert ev["evasion_on"] is True
    assert ev["proxy"] == "http://***@h:1"
    assert "pw" not in ev["proxy"]
    assert ev["fingerprint"] == "fp_9f31"
    assert ev["headless"] is False


def test_log_failure_appends_not_overwrites(tmp_path):
    dest = tmp_path / "failure_forensics.jsonl"
    assert F.log_failure(path=str(dest), kind="failed", task_id="a") is True
    assert F.log_failure(path=str(dest), kind="bot_wall", task_id="b") is True
    recs = _read_records(dest)
    assert [r["task_id"] for r in recs] == ["a", "b"]


def test_log_failure_never_raises(tmp_path):
    dest = tmp_path / "failure_forensics.jsonl"
    # un-serialisable payload (dict() of a bare object raises TypeError):
    # must return False, not raise
    assert F.log_failure(path=str(dest), kind="bot_wall",
                         evasion=object()) is False
    # no partial line was written
    assert not dest.exists() or dest.read_text().strip() == ""


# --------------------------------------------------------------------------- #
# runner funnel
# --------------------------------------------------------------------------- #
def test_deny_writes_bot_wall_record(tmp_path):
    dest = tmp_path / "failure_forensics.jsonl"
    r = _runner(dest)
    bundle = r._forensics_bundle(
        tid="t9", task={"id": "t9", "url": "https://www.target.com/p/x/1"},
        page=Page(), site="target", attempt=2, kind="bot_wall",
        classification="blocked", detector="robot or human",
        action="aborted_before_add_to_cart", outcome="denied:blocked",
        ev_snapshot=F.evasion_snapshot(
            evasion_on=True, proxy=None, fingerprint="fp_9f31",
            headless=False),
    )
    shot = r._deny(
        "t9", Page(),
        "aborted before add-to-cart — page is a bot-protection challenge",
        {"status": "blocked",
         "reason": "page is a bot-protection challenge, not the product"},
        forensics=bundle,
    )
    # the denial itself is unchanged: screenshot path returned, task failed
    assert shot.endswith("-t9-denied.png")
    assert r.task_store.runtime["t9"]["status"] == "failed"
    recs = _read_records(dest)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "bot_wall"
    assert rec["classification"] == "blocked"
    assert rec["detector"] == "robot or human"
    assert rec["attempt"] == 2
    assert rec["site"] == "target"
    assert rec["screenshot"] == shot
    assert rec["evasion"]["proxy"] == "direct"
    assert rec["evasion"]["evasion_on"] is True


def test_deny_without_forensics_writes_nothing(tmp_path):
    dest = tmp_path / "failure_forensics.jsonl"
    r = _runner(dest)
    shot = r._deny("t9", Page(), "some denial", {"status": "blocked"})
    assert shot.endswith("-t9-denied.png")
    assert not dest.exists()
