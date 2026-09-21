"""Lifecycle tests for BrowserSession.

These launch a real (ephemeral, headless) Chromium, so they need
`python -m playwright install chromium`. They skip cleanly if it's missing.
No persistent profile, no network — just start/reuse/close behaviour.
Run: `.venv/bin/python tests/test_browser_session.py`
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_session import BrowserSession

# Ephemeral (profile_dir=None) so `browser` is a real object, not None, and we
# never touch the committed persistent profile.
EPHEMERAL = dict(profile_dir=None, headless=True)


def test_session_starts():
    session = BrowserSession(**EPHEMERAL)
    try:
        browser, context, page = session.start().unpack()
        assert browser is not None, "ephemeral launch should return a Browser"
        assert context is not None
        assert page is not None
    finally:
        session.close()


def test_persistent_context_launches(tmp_profile="/tmp/bs_test_profile"):
    # In Playwright 1.60 a persistent context exposes a real context.browser
    # (older versions returned None). Either way the session must come up and
    # tear down cleanly against a persistent profile dir.
    session = BrowserSession(profile_dir=tmp_profile, headless=True)
    try:
        browser, context, page = session.start().unpack()
        assert context is not None
        assert page is not None
        assert browser is not None  # true as of pw 1.60; see note above
    finally:
        session.close()


def test_reuses_existing_context_even_with_no_pages():
    # Regression: the old guard `if self._context and self._context.pages`
    # fell through when every page was closed and launched a SECOND browser.
    session = BrowserSession(**EPHEMERAL)
    try:
        _, context1, page1 = session.start().unpack()
        page1.close()  # close every page
        assert not session.context.pages
        _, context2, _ = session.start().unpack()
        assert context1 is context2, "must reuse the context, not relaunch"
        assert len(session.context.pages) == 1
    finally:
        session.close()


def test_close_is_idempotent_and_reusable():
    session = BrowserSession(**EPHEMERAL)
    session.start()
    session.close()
    session.close()  # must not throw
    # Object is reusable after close() because state was reset to None.
    session.start()
    assert session.page is not None
    session.close()


TESTS = [
    test_session_starts,
    test_persistent_context_launches,
    test_reuses_existing_context_even_with_no_pages,
    test_close_is_idempotent_and_reusable,
]


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print("SKIP: playwright not installed")
        return 0

    failures = 0
    for t in TESTS:
        try:
            t()
            print(f"ok   {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e!r}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
