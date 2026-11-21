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
import signal
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

# When Chobot (re)starts, replace any bridge already on the port with a fresh
# process instead of adopting it. This guarantees the bridge reloads the current
# code and SYSBOT_API_KEY on every restart (an adopted/leftover bridge otherwise
# keeps serving a stale key and 401s all callers). Set to 0 to keep the old
# adopt-and-reuse behavior (e.g. if the bridge is run standalone via run.bat).
REPLACE_ON_START = os.getenv("SYSBOT_BRIDGE_REPLACE_ON_START", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)



def _pids_on_port(port: int) -> list[int]:
    """Best-effort: PIDs holding a LISTENING socket on the given TCP port."""
    pids: set[int] = set()
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                # Proto  Local Address  Foreign Address  State  PID
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
                    try:
                        pids.add(int(parts[4]))
                    except ValueError:
                        pass
        else:
            for cmd in (["ss", "-ltnpH"], ["lsof", f"-nPiTCP:{port}", "-sTCP:LISTEN", "-t"]):
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
                except (FileNotFoundError, OSError, subprocess.SubprocessError):
                    continue
                if cmd[0] == "lsof":
                    for tok in out.split():
                        if tok.isdigit():
                            pids.add(int(tok))
                else:  # ss
                    for line in out.splitlines():
                        if f":{port} " in line or line.rstrip().endswith(f":{port}"):
                            m = line.split("pid=")
                            if len(m) > 1:
                                num = "".join(ch for ch in m[1] if ch.isdigit())
                                if num:
                                    pids.add(int(num))
                if pids:
                    break
    except Exception as exc:  # pragma: no cover - diagnostic only
        logger.debug("[BRIDGE] port scan failed: %s", exc)
    pids.discard(os.getpid())  # never target ourselves
    return sorted(pids)


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
            os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        logger.warning("[BRIDGE] Failed to kill PID %d on bridge port: %s", pid, exc)


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

    def _replace_stale_bridge(self) -> bool:
        """Kill any process already listening on the bridge port so we can spawn
        a fresh one. Returns True once the port is free."""
        pids = _pids_on_port(self.port)
        if not pids:
            return not _port_in_use(self.host, self.port)
        logger.warning(
            "[BRIDGE] Replacing existing bridge on %s:%d (PID %s) with a fresh process "
            "so it reloads current code and SYSBOT_API_KEY.",
            self.host, self.port, ", ".join(map(str, pids)),
        )
        for pid in pids:
            _kill_pid(pid)
        for _ in range(25):  # wait up to ~5s for the port to free
            if not _port_in_use(self.host, self.port):
                return True
            time.sleep(0.2)
        return not _port_in_use(self.host, self.port)

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
                    # On (re)start, replace any bridge already on the port with a
                    # fresh one so it reloads current code and the API key.
                    if _port_in_use(self.host, self.port) and REPLACE_ON_START:
                        self._replace_stale_bridge()
                    if _port_in_use(self.host, self.port):
                        # Still occupied (replace disabled or failed) -> adopt it.
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
