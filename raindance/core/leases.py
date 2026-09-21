"""Exclusive leases on browser profiles, so parallel tasks stay independent.

Two layers, for two different failure modes:

``ProfileLeases`` (below) is an in-process guard — a Python-level lock that
stops two TASKS in the SAME running app from opening the same profile at
once. It cannot see anything outside this process.

``CrossProcessGuard`` (bottom of this file) closes the gap that leaves open:
a second `python app.py`, or a Chromium process orphaned by a crash that
never released its profile. Verified empirically before writing this —
Playwright's ``launch_persistent_context`` does NOT itself refuse a second
launch on an already-open profile directory; it succeeds silently, which
means nothing native stops two processes writing the same cookie jar and
cart state at once. The guard is deliberately FAIL-OPEN: if it cannot
determine the lock file's owner is genuinely alive (permissions error,
corrupt file, an OS where the liveness check is unavailable), it warns and
lets the launch proceed rather than blocking a normal single-instance run
over an uncertain read of a plain text file. A false "let it through" costs
what launching without this guard already cost before it existed; a false
"block a solitary user" would be a regression this change must never cause.

Why this exists. A profile owns three things that must not be shared while a
task is running: a persistent Chromium ``user_data_dir`` (cookies, session,
**and the shopping cart**), a payment card, and a shipping address. Two tasks
holding the same profile at the same time are not two independent attempts —
they are two drivers of one cart, and whichever reaches place-order buys
whatever both of them put in it.

That was the live behaviour: ``execute_queue.materialize`` assigned
``profiles[0]`` to every row that had not picked a profile explicitly, so a
queue of eight listings ran eight browser windows against one cart.

The rule here is simple and enforced rather than documented: one running task
per profile. A second task wanting the same profile waits for the lease, and
gives up cleanly if it does not get it in time rather than proceeding into a
shared cart. Tasks on *different* profiles never touch each other — they run
fully in parallel, each with its own card, address, cookies and cart, which is
the whole point of having profiles.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Optional


class LeaseBusy(RuntimeError):
    """Raised when a profile's lease could not be acquired in time."""

    def __init__(self, profile_id: str, holder: Optional[str], waited: float):
        self.profile_id = profile_id
        self.holder = holder
        self.waited = waited
        super().__init__(
            f"profile {profile_id} is in use by task {holder or '(unknown)'} "
            f"after waiting {waited:.1f}s — refusing to share a cart"
        )


class ProfileLeases:
    """Process-wide registry of who currently holds each profile."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._holders: dict[str, str] = {}

    def _lock_for(self, profile_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(profile_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[profile_id] = lock
            return lock

    def holder(self, profile_id: str) -> Optional[str]:
        with self._guard:
            return self._holders.get(profile_id)

    def active(self) -> dict[str, str]:
        with self._guard:
            return dict(self._holders)

    def acquire(self, profile_id: str, task_id: str, timeout: float = 0.0) -> bool:
        if not profile_id:
            return True                      # no profile → nothing shared
        lock = self._lock_for(profile_id)
        got = lock.acquire(timeout=timeout) if timeout > 0 else lock.acquire(blocking=False)
        if not got:
            return False
        with self._guard:
            self._holders[profile_id] = task_id
        return True

    def release(self, profile_id: str, task_id: str) -> None:
        if not profile_id:
            return
        with self._guard:
            if self._holders.get(profile_id) == task_id:
                self._holders.pop(profile_id, None)
            else:
                return                       # not ours; never release someone else's
        lock = self._locks.get(profile_id)
        if lock is not None and lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass

    @contextmanager
    def hold(self, profile_id: str, task_id: str, timeout: float = 0.0):
        """Hold a profile for the duration of a block, or raise LeaseBusy."""
        started = time.monotonic()
        if not self.acquire(profile_id, task_id, timeout=timeout):
            raise LeaseBusy(profile_id, self.holder(profile_id),
                            time.monotonic() - started)
        try:
            yield
        finally:
            self.release(profile_id, task_id)


# The orchestrator, the runner and the UI all consult the same registry.
LEASES = ProfileLeases()


def assign_profiles(rows: list, available: list[str], *,
                    get=lambda r: r.get("profile_id")) -> list[str]:
    """Choose a profile per row, spreading rows across distinct profiles.

    A row that names a profile keeps it. Rows that do not are dealt the
    remaining profiles round-robin, so a queue of listings runs on as many
    independent identities as exist instead of piling onto ``profiles[0]``.
    Returns one profile id per row, parallel to ``rows``.
    """
    if not available:
        return ["" for _ in rows]

    explicit = {get(r) for r in rows if get(r)}
    # Prefer handing out profiles nobody asked for by name, so an explicit
    # choice is not duplicated by an automatic one while spares exist.
    pool = [p for p in available if p not in explicit] or list(available)

    out: list[str] = []
    turn = 0
    for r in rows:
        chosen = get(r)
        if chosen:
            out.append(chosen)
            continue
        out.append(pool[turn % len(pool)])
        turn += 1
    return out


class CrossProcessGuard:
    """Fail-open advisory lock on a profile's user_data_dir, PID-verified.

    Not a replacement for ProfileLeases — it runs ADDITIONALLY, right where
    browser_factory.py is about to call launch_persistent_context, and covers
    only the case ProfileLeases structurally cannot: a session started by a
    different OS process. Uses a small JSON file inside the profile directory
    itself, never touches anything else in it, and is safe to have never
    existed — a directory with no lock file behaves exactly as it always has.
    """

    FILENAME = ".raindance_session.lock"

    @staticmethod
    def _pid_alive(pid: int) -> Optional[bool]:
        """True/False if we can tell, None if the platform won't say."""
        import os
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by someone else — still alive
        except Exception:
            return None  # unsupported (e.g. some Windows configurations)

    @classmethod
    def check(cls, user_data_dir: str) -> Optional[str]:
        """None if the launch may proceed; else a human-readable refusal.

        Never raises — any failure to read or parse the lock file is treated
        as "no usable lock", the same as no file at all, per the fail-open
        rule in the module docstring.
        """
        import json
        import os

        path = os.path.join(user_data_dir, cls.FILENAME)
        try:
            if not os.path.exists(path):
                return None
            with open(path, "r") as fh:
                data = json.load(fh)
            pid = int(data.get("pid", -1))
            if pid == os.getpid():
                return None  # our own prior lock in this same process
            alive = cls._pid_alive(pid)
            if alive is False:
                return None  # confirmed stale — the writer is gone
            if alive is None:
                return None  # can't tell; fail open rather than guess
            return (f"profile directory {user_data_dir!r} has an active "
                    f"session from another process (pid {pid}) — closing "
                    f"that session (or its browser window) will release it")
        except Exception:
            return None

    @classmethod
    def acquire(cls, user_data_dir: str) -> None:
        """Write our PID into the lock file. Best-effort: a write failure
        (read-only filesystem, permissions) is silently ignored — the guard
        degrades to "absent", not to blocking a launch over its own I/O."""
        import json
        import os
        import time

        try:
            os.makedirs(user_data_dir, exist_ok=True)
            path = os.path.join(user_data_dir, cls.FILENAME)
            with open(path, "w") as fh:
                json.dump({"pid": os.getpid(), "acquired": time.time()}, fh)
        except Exception:
            pass

    @classmethod
    def release(cls, user_data_dir: str) -> None:
        """Remove our lock file, but ONLY if it is still ours — a launch that
        lost the race and got refused must never delete the winner's lock."""
        import json
        import os

        path = os.path.join(user_data_dir, cls.FILENAME)
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
            if int(data.get("pid", -1)) == os.getpid():
                os.remove(path)
        except Exception:
            pass
