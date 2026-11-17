"""
Start and supervise sysbot_bridge/server.py alongside Chobot.

The bridge stays a separate process (its own event loop and port). Chobot only:
  * starts it if nothing is listening on the bridge port yet (another Chobot process or
    run.bat may already own it, in which case that one is reused),
  * restarts it with backoff if it exits,
  * stops it on a clean Chobot shutdown, but leaves it running across OTA restarts.

Always on. Output goes to logs/sysbot_bridge.log.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("SysBotBridge")

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = REPO_ROOT / "sysbot_bridge"
LOG_PATH = REPO_ROOT / "logs" / "sysbot_bridge.log"


def _bridge_env_value(key: str, default: str) -> str:
    if os.getenv(key):
        return os.environ[key].strip()
    env_file = BRIDGE_DIR / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return default


def _port_in_use(host: str, port: int) -> bool:
    probe = "127.0.0.1" if host in ("0.0.0.0", "", "localhost") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((probe, port)) == 0


class BridgeSupervisor:
    def __init__(self, stop_event: threading.Event):
        self.stop_event = stop_event
        self.host = _bridge_env_value("BRIDGE_HOST", "127.0.0.1")
        self.port = int(_bridge_env_value("BRIDGE_PORT", "8200"))
        self.proc: subprocess.Popen | None = None
        self._log_fh = None
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        if not (BRIDGE_DIR / "server.py").is_file():
            logger.warning("[BRIDGE] sysbot_bridge/server.py not found; autostart skipped.")
            return
        self._thread = threading.Thread(target=self._run, name="SysBotBridgeSupervisor", daemon=True)
        self._thread.start()

    def _spawn(self) -> None:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        self._log_fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} starting bridge (chobot pid {os.getpid()}) =====\n")
        flags = 0
        if os.name == "nt":
            # Own process group: a Ctrl+C in Chobot's console doesn't kill the bridge mid-request;
            # we stop it ourselves on a clean shutdown.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=str(BRIDGE_DIR),
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=flags,
        )
        logger.info("[BRIDGE] Started sysbot_bridge (PID %d) on %s:%d, log: %s",
                    self.proc.pid, self.host, self.port, LOG_PATH)

    def _close_log(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def _run(self) -> None:
        backoff = 3.0
        adopted_logged = False
        while not self.stop_event.is_set():
            exited = None
            with self._lock:
                proc = self.proc
                if proc is not None and proc.poll() is not None:
                    exited = (proc.returncode, time.monotonic() - self._started_at)
                    self.proc = None
                    self._close_log()
            if exited:
                code, lived = exited
                logger.warning("[BRIDGE] sysbot_bridge exited (code %s) after %.0fs; see %s",
                               code, lived, LOG_PATH)
                backoff = 3.0 if lived > 60 else min(backoff * 2, 60.0)
                if self.stop_event.wait(backoff):
                    break

            with self._lock:
                if self.proc is None and not self.stop_event.is_set():
                    if _port_in_use(self.host, self.port):
                        if not adopted_logged:
                            logger.info("[BRIDGE] Port %d already in use; reusing the running bridge.", self.port)
                            adopted_logged = True
                    else:
                        adopted_logged = False
                        try:
                            self._started_at = time.monotonic()
                            self._spawn()
                        except Exception as exc:
                            logger.error("[BRIDGE] Failed to start sysbot_bridge: %s", exc)
                            self._close_log()
            self.stop_event.wait(5.0)

    def stop(self, keep_running: bool = False) -> None:
        """Stop the bridge we started. keep_running=True leaves it up (OTA restart)."""
        with self._lock:
            proc, self.proc = self.proc, None
            if proc is None or proc.poll() is not None:
                self._close_log()
                return
            if keep_running:
                logger.info("[BRIDGE] Leaving sysbot_bridge (PID %d) running for the restarted process.", proc.pid)
                self._close_log()
                return
            logger.info("[BRIDGE] Stopping sysbot_bridge (PID %d)...", proc.pid)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
            self._close_log()


def start_bridge(stop_event: threading.Event) -> BridgeSupervisor:
    sup = BridgeSupervisor(stop_event)
    sup.start()
    return sup
