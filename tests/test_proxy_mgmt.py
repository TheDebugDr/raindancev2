"""Tests for the managed proxy inventory (add/remove/list).

No network, no browser. The TCP/HTTP probes are exercised only through
their credential-scrubbing — never against a live proxy.

Run: cd /Users/nick/raindancev2 && python -m pytest tests/test_proxy_mgmt.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from raindance.evasion.proxy_groups import ProxyGroupManager
from raindance.evasion.proxy_inventory import ProxyInventory
from raindance.evasion.proxy_manager import ProxyManager


class FakeSettings:
    """Minimal stand-in for raindance.core.settings.Settings."""
    def __init__(self):
        self.data = {}
        self.saved = 0

    def save(self):
        self.saved += 1


def _inv():
    return ProxyInventory(FakeSettings())


# -- add --------------------------------------------------------------- #

def test_add_valid_proxy_returns_masked_entry():
    inv = _inv()
    got = inv.add_proxy("http://user1:s3cret@1.2.3.4:8080",
                        group="residential", label="resi-01", notes="provider X")
    assert got["group"] == "residential"
    assert got["label"] == "resi-01"
    assert got["host"] == "1.2.3.4"
    assert got["port"] == 8080
    assert got["notes"] == "provider X"
    assert got["sticky"] is None
    assert got["added_at"]  # timestamp recorded
    # the masked form must not leak credentials
    assert "s3cret" not in got["url"]
    assert "1.2.3.4" in got["url"]


def test_add_accepts_legacy_bare_forms():
    inv = _inv()
    got = inv.add_proxy("5.6.7.8:3128", label="bare")
    assert got["host"] == "5.6.7.8" and got["port"] == 3128
    got = inv.add_proxy("9.9.9.9:1080:u:p")
    assert got["host"] == "9.9.9.9" and got["port"] == 1080


@pytest.mark.parametrize("bad", [
    "not a proxy!!",          # junk
    "http://host:notaport",   # non-numeric port
    "http://host:99999",      # port out of range
    "http://host",            # no port
    "gopher://1.2.3.4:70",    # unsupported scheme
    "",                       # empty
])
def test_add_rejects_bad_formats(bad):
    inv = _inv()
    with pytest.raises(ValueError):
        inv.add_proxy(bad)


def test_add_error_never_echoes_raw_credentials():
    inv = _inv()
    try:
        inv.add_proxy("http://u:pw@999.999.999.999:99999")
    except ValueError as exc:
        assert "pw" not in str(exc) or "***" in str(exc)
        assert "u:pw@" not in str(exc)
    else:
        pytest.fail("expected ValueError")


def test_add_duplicate_rejected():
    inv = _inv()
    inv.add_proxy("http://u:p@1.2.3.4:8080", label="first")
    with pytest.raises(ValueError, match="duplicate"):
        inv.add_proxy("http://u:p@1.2.3.4:8080", label="second")


def test_add_duplicate_label_rejected():
    inv = _inv()
    inv.add_proxy("http://u:p@1.2.3.4:8080", label="one")
    with pytest.raises(ValueError, match="label"):
        inv.add_proxy("http://u:p@5.6.7.8:8080", label="one")


def test_add_autocreates_group_with_shared_defaults():
    inv = _inv()
    inv.add_proxy("http://u:p@1.2.3.4:8080", group="eu-resi")
    assert "eu-resi" in inv.groups()
    shared = inv.group_settings("eu-resi")
    assert shared["sticky"] is True
    assert "proxies" not in shared  # per-proxy data stays out of shared settings


def test_group_settings_stay_in_proxy_groups():
    inv = _inv()
    inv.add_proxy("http://u:p@1.2.3.4:8080", group="residential")
    cfg = inv.settings.data["proxy_groups"]["residential"]
    assert "sticky" in cfg and "file" in cfg          # shared settings kept
    entry = cfg["proxies"][0]
    assert entry["label"] == "" and entry["sticky"] is None  # per-proxy


# -- list -------------------------------------------------------------- #

def _flatten_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten_strings(v)


def test_list_never_leaks_credentials():
    inv = _inv()
    inv.add_proxy("http://alice:hunter2@1.2.3.4:8080", label="a")
    inv.add_proxy("socks5://bob:correct-horse@5.6.7.8:1080", label="b")
    listed = inv.list_proxies()
    assert len(listed) == 2
    for text in _flatten_strings(listed):
        assert "hunter2" not in text
        assert "correct-horse" not in text
        assert "alice:hunter2@" not in text


def test_list_group_filter():
    inv = _inv()
    inv.add_proxy("http://u:p@1.2.3.4:8080", group="a")
    inv.add_proxy("http://u:p@5.6.7.8:8080", group="b")
    assert len(inv.list_proxies("a")) == 1
    assert len(inv.list_proxies()) == 2


# -- remove ------------------------------------------------------------ #

def test_remove_by_label_and_by_url():
    inv = _inv()
    inv.add_proxy("http://u:p1@1.2.3.4:8080", label="one")
    inv.add_proxy("http://u:p2@5.6.7.8:8080")
    removed = inv.remove_proxy("one")
    assert removed["label"] == "one"
    removed = inv.remove_proxy("http://u:p2@5.6.7.8:8080")
    assert removed["host"] == "5.6.7.8"
    assert inv.list_proxies() == []


def test_remove_missing_raises_keyerror():
    inv = _inv()
    with pytest.raises(KeyError):
        inv.remove_proxy("nope")


def test_remove_scoped_to_group():
    inv = _inv()
    inv.add_proxy("http://u:p@1.2.3.4:8080", group="a", label="x")
    with pytest.raises(KeyError):
        inv.remove_proxy("x", group="b")
    assert len(inv.list_proxies("a")) == 1


# -- metadata round-trip through the group manager --------------------- #

def test_sticky_override_flows_into_group_manager():
    inv = _inv()
    inv.add_proxy("http://u:p@1.2.3.4:8080", group="residential",
                  label="pinned", sticky=False)
    pgm = ProxyGroupManager()
    pgm.load_from_settings(inv.settings.data)
    mgr = pgm.get_manager("residential")
    assert mgr is not None and mgr.count == 1
    server = mgr.proxies[0]["server"]
    assert mgr._sticky_overrides[server] is False
    assert mgr.meta_for(server)["label"] == "pinned"
    # still usable — never None-crash on the task path
    assert mgr.get_proxy("acct-1") is not None


def test_legacy_string_entries_still_load():
    settings = FakeSettings()
    settings.data["proxy_groups"] = {
        "default": {"file": "", "proxies": ["1.2.3.4:8080"], "sticky": True},
    }
    pgm = ProxyGroupManager()
    pgm.load_from_settings(settings.data)
    assert pgm.count("default") == 1
    inv = ProxyInventory(settings)
    listed = inv.list_proxies("default")
    assert listed[0]["label"] == "" and listed[0]["sticky"] is None


def test_proxy_manager_from_lines_accepts_managed_dicts():
    mgr = ProxyManager.from_lines([
        {"url": "http://u:p@1.2.3.4:8080", "label": "m", "sticky": True},
        "5.6.7.8:3128",
        "junk line!!",
    ])
    assert mgr.count == 2
    assert mgr.meta_for("http://1.2.3.4:8080")["label"] == "m"
    assert mgr._sticky_overrides["http://1.2.3.4:8080"] is True


# -- empty pool stays safe --------------------------------------------- #

def test_empty_pool_behaves():
    inv = _inv()
    assert inv.list_proxies() == []
    assert inv.groups() == []
    pgm = ProxyGroupManager()
    pgm.load_from_settings(inv.settings.data)  # no crash, default synthesized
    mgr = pgm.get_manager("default")
    assert mgr is not None and mgr.count == 0
    assert mgr.get_proxy("acct") is None     # task path exits direct
    # adding to an empty group just works
    inv.add_proxy("http://u:p@1.2.3.4:8080", group="default")
    assert len(inv.list_proxies("default")) == 1


# -- persistence ------------------------------------------------------- #

def test_save_delegates_to_settings(tmp_path):
    from raindance.core.settings import Settings
    settings = Settings(path=tmp_path / "config.json")
    inv = ProxyInventory(settings)
    inv.add_proxy("http://u:p@1.2.3.4:8080", group="residential", label="r1")
    inv.save()
    reloaded = Settings(path=tmp_path / "config.json")
    entry = reloaded.data["proxy_groups"]["residential"]["proxies"][0]
    assert entry["url"] == "http://u:p@1.2.3.4:8080"
    assert entry["label"] == "r1"
    # raw credentials are stored (config.json is gitignored, like proxies.txt
    # was) but the inventory's read APIs never surface them
    listed = ProxyInventory(reloaded).list_proxies()
    assert "u:p@" not in listed[0]["url"]


def test_sync_rebuilds_live_managers():
    settings = FakeSettings()
    inv = ProxyInventory(settings)
    live_mgr = ProxyManager([])
    live_groups = ProxyGroupManager()
    live_groups.load_from_settings(settings.data)
    inv.add_proxy("http://u:p@1.2.3.4:8080", group="residential", label="r1")
    inv.sync(proxy_manager=live_mgr, proxy_groups=live_groups)
    assert live_groups.count("residential") == 1
    assert live_groups.get_manager("residential").meta_for(
        "http://1.2.3.4:8080")["label"] == "r1"


# -- probe scrubbing --------------------------------------------------- #

def test_probe_scrub_removes_credentials():
    text = ("Connect tunnel failed for "
            "http://alice:hunter2@1.2.3.4:8080/ via proxy")
    clean = ProxyInventory._scrub(
        text, {"username": "alice", "password": "hunter2"})
    assert "hunter2" not in clean
    assert "alice" not in clean
    assert "1.2.3.4" in clean
