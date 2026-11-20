"""OTA update shared by Discord /update and the Flask /dashboard/api/update endpoint.

Pulls the latest code with git and asks main() to restart the process. main() does the
os.execv() itself from the main thread once every service has shut down.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger("OTAUpdate")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GIT_PULL_TIMEOUT_SECONDS = 30

# Only these Discord users may run an update (Discord command or website), regardless of roles.
UPDATE_ALLOWED_USER_IDS = frozenset({712983261684432897, 660198924946243609})

_update_lock = threading.Lock()
_restart_requested = threading.Event()
_stop_hook: Callable[[], None] | None = None


def is_update_allowed_user(user_id) -> bool:
    try:
        return int(str(user_id or "").strip()) in UPDATE_ALLOWED_USER_IDS
    except ValueError:
        return False


def find_git_executable() -> str | None:
    """Find git executable from PATH or common installation paths."""
    git_cmd = shutil.which("git")
    if git_cmd:
        return git_cmd

    candidates = [
        # Linux / Unix
        "/usr/bin/git",
        "/usr/local/bin/git",
        "/bin/git",
        # Windows
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Git\cmd\git.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c) and (os.name == "nt" or os.access(c, os.X_OK)):
            return c
    return None


def run_git_pull() -> dict:
    """Run `git pull` in the project root.

    Returns {ok, updated, returncode, output, error}. `updated` is True only when new
    code was pulled (so a restart is worth doing). Concurrent calls are refused.
    """
    if not _update_lock.acquire(blocking=False):
        return {"ok": False, "updated": False, "returncode": None, "output": "",
                "error": "Another update is already running."}
    try:
        git_bin = find_git_executable()
        if not git_bin:
            return {"ok": False, "updated": False, "returncode": None, "output": "",
                    "error": "Git executable not found on the server."}
        try:
            result = subprocess.run(
                [git_bin, "pull"],
                capture_output=True,
                text=True,
                timeout=GIT_PULL_TIMEOUT_SECONDS,
                cwd=str(PROJECT_ROOT),
                # Force English output so "Already up to date" can be detected.
                env={**os.environ, "LANG": "C", "LC_ALL": "C"},
            )
        except Exception as exc:
            logger.exception("[OTA] git pull failed: %s", exc)
            return {"ok": False, "updated": False, "returncode": None, "output": "", "error": str(exc)}

        output = result.stdout.strip() or result.stderr.strip() or "No output."
        ok = result.returncode == 0
        return {
            "ok": ok,
            "updated": ok and "already up to date" not in output.lower(),
            "returncode": result.returncode,
            "output": output,
            "error": None if ok else f"git pull exited with code {result.returncode}",
        }
    finally:
        _update_lock.release()


def set_stop_hook(hook: Callable[[], None]) -> None:
    """main() registers how to shut every service down (sets its STOP_EVENT)."""
    global _stop_hook
    _stop_hook = hook


def request_restart(delay_seconds: float = 0.0) -> bool:
    """Ask main() to shut down and re-exec the process. False if main() never registered."""
    if _stop_hook is None:
        logger.warning("[OTA] Restart requested but no stop hook is registered; not restarting.")
        return False
    _restart_requested.set()

    def _stop():
        logger.info("[OTA] Shutting services down for restart...")
        _stop_hook()

    if delay_seconds > 0:
        timer = threading.Timer(delay_seconds, _stop)
        timer.daemon = True
        timer.start()
    else:
        _stop()
    return True


def restart_was_requested() -> bool:
    return _restart_requested.is_set()
