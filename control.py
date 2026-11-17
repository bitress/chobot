"""
control.py - single-file remote controller for a Chopaeng SysBot island.

Talks to the SysBot.ACNHOrders HTTP API, either directly (http://127.0.0.1:8201)
or through the sysbot_bridge / Cloudflare tunnel (…/islands/{island}).

What it can do (all endpoints already allowed by sysbot_bridge):
  * Press controller buttons  -> POST /api/click   {"button", "count", "delay_ms"}
  * Bot actions               -> POST /api/control {"action": start|stop|restart|pause|resume|
                                                     screen|detach|newdodo|mash|map}
  * In-game chat              -> POST /api/speak   {"message"}
  * Status / Dodo             -> GET  /api/status

Walking (analog stick) is NOT in the current SysBot API. The stick pad below sends
POST /api/stick {"stick": "L"|"R", "x", "y", "duration_ms"} and reports clearly if the
bot / bridge doesn't support it yet.

Usage:
  python control.py                      # uses .env (SYSBOT_URL_TEMPLATE / SYSBOT_API_KEY)
  python control.py --island Hiraya
  python control.py --url http://127.0.0.1:8201          # talk to one SysBot directly
  python control.py --template http://127.0.0.1:8200/islands/{island} --key SECRET

Keyboard (window focused):
  Z=A  X=B  C=X  V=Y   Q=L  E=R  1=ZL  3=ZR   Enter=PLUS  Backspace=MINUS
  Arrow keys=D-pad     H=HOME            W/A/S/D = left stick (if supported)
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent

SUB_ISLANDS = [
    "Hiraya", "Tagumpay", "Bahaghari", "Alapaap", "Bituin", "Lakan", "Galak", "Aruga",
    "Likha", "Dalisay", "Marahuyo", "Diwa", "Bonita", "Gabay", "Dakila", "Kalangitan",
    "Malaya", "Pangarap", "Giliw", "Adhika",
]
FREE_ISLANDS = [
    "Kakanggata", "Kalawakan", "Kundiman", "Kilig", "Bathala", "Dalangin", "Gunita",
    "Kaulayaw", "Tala", "Sinagtala", "Tinig", "Tadhana", "Maharlika", "Pagsamo", "Harana",
    "Pagsuyo", "Matahom", "Paraluman", "Marilag", "Babaylan", "Amihan", "Silakbo",
    "Banaag", "Dangal", "Kariktan", "Sinag", "Giting",
]
ORDER_ISLANDS = ["Sinta"]
ALL_ISLANDS = SUB_ISLANDS + FREE_ISLANDS + ORDER_ISLANDS

# SysBot.Base SwitchButton names
KEYMAP = {
    "z": "A", "x": "B", "c": "X", "v": "Y",
    "q": "L", "e": "R", "1": "ZL", "3": "ZR",
    "Return": "PLUS", "BackSpace": "MINUS", "h": "HOME",
    "Up": "DUP", "Down": "DDOWN", "Left": "DLEFT", "Right": "DRIGHT",
}
STICK_KEYS = {"w": (0, 1), "s": (0, -1), "a": (-1, 0), "d": (1, 0)}

# (action, label, confirm text or None)
ACTIONS = [
    ("start", "Start", None),
    ("stop", "Stop", "Stop the bot loop and disconnect the Switch?"),
    ("restart", "Restart", "Restart the console loop?"),
    ("pause", "Pause", None),
    ("resume", "Resume", None),
    ("screen", "Screen", None),
    ("detach", "Detach", None),
    ("newdodo", "New Dodo", "Restart the game for a new Dodo? Visitors will be disconnected."),
    ("mash", "Mash B", None),
    ("map", "Map refresh", None),
]


# ─── Config ─────────────────────────────────────────────────────────────────

def load_env() -> dict[str, str]:
    """Minimal .env reader (repo root, then sysbot_bridge/.env). Real env vars win."""
    values: dict[str, str] = {}
    for p in (HERE / "sysbot_bridge" / ".env", HERE / ".env"):
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip("'\"")
    values.update({k: v for k, v in os.environ.items() if k.startswith(("SYSBOT_", "CF_ACCESS_"))})
    return values


# ─── HTTP client ────────────────────────────────────────────────────────────

class SysBot:
    def __init__(self, template: str, key: str, cf_id: str = "", cf_secret: str = ""):
        self.template = template
        self.key = key
        self.cf_id = cf_id
        self.cf_secret = cf_secret
        self.island = ALL_ISLANDS[0]

    def base(self) -> str:
        if "{island}" in self.template:
            return self.template.format(island=self.island.lower()).rstrip("/")
        return self.template.rstrip("/")

    def request(self, method: str, path: str, payload: dict | None = None, timeout: float = 15) -> tuple[int, dict]:
        headers = {"Accept": "application/json", "User-Agent": "chopaeng-control/1.0"}
        if self.key:
            headers["X-API-Key"] = self.key
        if self.cf_id and self.cf_secret:
            headers["CF-Access-Client-Id"] = self.cf_id
            headers["CF-Access-Client-Secret"] = self.cf_secret
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base() + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read())
        except (urllib.error.URLError, OSError) as exc:
            return 0, {"success": False, "error": f"unreachable: {getattr(exc, 'reason', exc)}"}

    def click(self, button: str, count: int = 1, delay_ms: int = 200):
        return self.request("POST", "/api/click", {"button": button, "count": count, "delay_ms": delay_ms})

    def stick(self, stick: str, x: float, y: float, duration_ms: int):
        # Normalised -1..1 -> Switch range -32767..32767
        return self.request("POST", "/api/stick", {
            "stick": stick, "x": int(x * 32767), "y": int(y * 32767), "duration_ms": duration_ms,
        })

    def action(self, name: str):
        return self.request("POST", "/api/control", {"action": name})

    def speak(self, message: str):
        return self.request("POST", "/api/speak", {"message": message})

    def status(self):
        return self.request("GET", "/api/status", timeout=8)


def _parse(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text) if text else {}
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        first = text.splitlines()[0][:200] if text else ""
        if "<html" in text.lower():
            first = "HTML error page (tunnel / bridge down?)"
        return {"success": False, "error": first}


def describe(code: int, data: dict) -> tuple[bool, str]:
    ok = 200 <= code < 300 and data.get("success", True) is not False
    msg = data.get("message") or data.get("error") or data.get("status") or ""
    if not msg and data:
        msg = json.dumps(data)[:200]
    return ok, f"[{code or 'ERR'}] {msg}".strip()


# ─── GUI ────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self, bot: SysBot, island: str):
        super().__init__()
        self.bot = bot
        self.title("Chopaeng Control")
        self.minsize(720, 520)
        self.jobs: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.island_var = tk.StringVar(value=island)
        self.count_var = tk.IntVar(value=1)
        self.delay_var = tk.IntVar(value=200)
        self.hold_var = tk.IntVar(value=400)
        self.keys_var = tk.BooleanVar(value=True)
        self._build()
        self._on_island()
        threading.Thread(target=self._worker, daemon=True).start()
        self._drain()
        self.bind_all("<KeyPress>", self._on_key)

    # Commands run one at a time, in order, off the UI thread.
    def _worker(self):
        while True:
            label, fn = self.jobs.get()
            try:
                code, data = fn()
                ok, msg = describe(code, data)
                if label == "stick" and code in (403, 404, 405):
                    ok, msg = False, f"[{code}] stick movement not supported by this SysBot/bridge yet"
            except Exception as exc:  # noqa: BLE001
                ok, msg, data = False, f"error: {exc}", {}
            # Tk is not thread-safe: hand results to the UI thread via a queue.
            self.results.put((label, ok, msg, data))

    def _drain(self):
        try:
            while True:
                label, ok, msg, data = self.results.get_nowait()
                self._log(f"{label}: {msg}", ok)
                if label == "status":
                    self._show_status(data if isinstance(data, dict) else {})
        except queue.Empty:
            pass
        self.after(50, self._drain)

    def submit(self, label: str, fn):
        self._log(f"→ {label}", None)
        self.jobs.put((label, fn))

    # ── layout ──
    def _build(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Island").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.island_var, values=ALL_ISLANDS, width=16, state="readonly")
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_island())
        ttk.Button(top, text="Status", command=self._status).pack(side="left", padx=4)
        self.url_lbl = ttk.Label(top, foreground="#666")
        self.url_lbl.pack(side="left", padx=8)
        ttk.Checkbutton(top, text="Keyboard", variable=self.keys_var).pack(side="right")

        self.status_lbl = ttk.Label(self, padding=(8, 0), text="Status: —")
        self.status_lbl.pack(fill="x")

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)

        # Controller
        pad = ttk.LabelFrame(body, text="Controller (POST /api/click)", padding=8)
        pad.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        btn = lambda parent, b, r, c, w=5: ttk.Button(parent, text=b, width=w, command=lambda: self._press(b)).grid(row=r, column=c, padx=2, pady=2)

        sh = ttk.Frame(pad); sh.grid(row=0, column=0, columnspan=3, pady=(0, 6))
        for i, b in enumerate(("ZL", "L", "MINUS", "HOME", "CAPTURE", "PLUS", "R", "ZR")):
            btn(sh, b, 0, i, 7 if len(b) > 3 else 4)

        dp = ttk.Frame(pad); dp.grid(row=1, column=0, padx=8)
        ttk.Label(dp, text="D-pad").grid(row=0, column=0, columnspan=3)
        ttk.Button(dp, text="▲", width=4, command=lambda: self._press("DUP")).grid(row=1, column=1)
        ttk.Button(dp, text="◀", width=4, command=lambda: self._press("DLEFT")).grid(row=2, column=0)
        ttk.Button(dp, text="▶", width=4, command=lambda: self._press("DRIGHT")).grid(row=2, column=2)
        ttk.Button(dp, text="▼", width=4, command=lambda: self._press("DDOWN")).grid(row=3, column=1)

        face = ttk.Frame(pad); face.grid(row=1, column=2, padx=8)
        ttk.Label(face, text="Face").grid(row=0, column=0, columnspan=3)
        btn(face, "X", 1, 1, 4); btn(face, "Y", 2, 0, 4); btn(face, "A", 2, 2, 4); btn(face, "B", 3, 1, 4)

        sticks = ttk.Frame(pad); sticks.grid(row=1, column=1, padx=8)
        ttk.Label(sticks, text="Sticks").grid(row=0, column=0, columnspan=2)
        btn(sticks, "LSTICK", 1, 0, 7); btn(sticks, "RSTICK", 1, 1, 7)

        opts = ttk.Frame(pad); opts.grid(row=2, column=0, columnspan=3, pady=(8, 0))
        ttk.Label(opts, text="Count").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=50, width=4, textvariable=self.count_var).pack(side="left", padx=4)
        ttk.Label(opts, text="Delay ms").pack(side="left")
        ttk.Spinbox(opts, from_=50, to=5000, increment=50, width=6, textvariable=self.delay_var).pack(side="left", padx=4)

        # Movement
        mv = ttk.LabelFrame(pad, text="Walk (POST /api/stick — needs bot support)", padding=6)
        mv.grid(row=3, column=0, columnspan=3, pady=(10, 0), sticky="ew")
        for (dx, dy), (r, c), t in (((0, 1), (0, 1), "↑"), ((-1, 0), (1, 0), "←"), ((1, 0), (1, 2), "→"),
                                    ((0, -1), (2, 1), "↓"), ((-1, 1), (0, 0), "↖"), ((1, 1), (0, 2), "↗"),
                                    ((-1, -1), (2, 0), "↙"), ((1, -1), (2, 2), "↘")):
            ttk.Button(mv, text=t, width=4, command=lambda x=dx, y=dy: self._walk(x, y)).grid(row=r, column=c)
        ttk.Label(mv, text=" Hold ms").grid(row=1, column=3)
        ttk.Spinbox(mv, from_=100, to=5000, increment=100, width=6, textvariable=self.hold_var).grid(row=1, column=4)

        # Right column: actions + chat + log
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        act = ttk.LabelFrame(right, text="Bot actions (POST /api/control)", padding=6)
        act.pack(fill="x")
        for i, (key, label, confirm) in enumerate(ACTIONS):
            ttk.Button(act, text=label, command=lambda k=key, l=label, c=confirm: self._action(k, l, c)).grid(
                row=i // 5, column=i % 5, padx=2, pady=2, sticky="ew")

        chat = ttk.LabelFrame(right, text="Chat (POST /api/speak)", padding=6)
        chat.pack(fill="x", pady=8)
        self.chat_entry = ttk.Entry(chat)
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", lambda e: self._speak())
        ttk.Button(chat, text="Send", command=self._speak).pack(side="left", padx=4)

        logf = ttk.LabelFrame(right, text="Log", padding=4)
        logf.pack(fill="both", expand=True)
        self.log = tk.Text(logf, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("ok", foreground="#1a7f37")
        self.log.tag_config("err", foreground="#c62828")
        self.log.tag_config("info", foreground="#666")

    # ── handlers ──
    def _on_island(self):
        self.bot.island = self.island_var.get()
        self.url_lbl.config(text=self.bot.base())
        self.status_lbl.config(text="Status: —")
        self._status()

    def _press(self, button: str):
        count, delay = _int(self.count_var, 1), _int(self.delay_var, 200)
        suffix = f" x{count}" if count > 1 else ""
        self.submit(f"click {button}{suffix}", lambda: self.bot.click(button, count, delay))

    def _walk(self, x: float, y: float):
        if x and y:
            x, y = x * 0.7071, y * 0.7071
        hold = _int(self.hold_var, 400)
        self.submit("stick", lambda: self.bot.stick("L", x, y, hold))

    def _action(self, key: str, label: str, confirm: str | None):
        if confirm and not messagebox.askyesno(f"{label} — {self.bot.island}", confirm, parent=self):
            return
        self.submit(f"action {key}", lambda: self.bot.action(key))

    def _speak(self):
        msg = self.chat_entry.get().strip()
        if msg:
            self.chat_entry.delete(0, "end")
            self.submit("speak", lambda: self.bot.speak(msg))

    def _status(self):
        self.submit("status", self.bot.status)

    def _show_status(self, data: dict):
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        if not d or d.get("success") is False and len(d) <= 2:
            self.status_lbl.config(text=f"Status: offline — {d.get('error', '')}")
            return
        bits = []
        for k in ("island_name", "island", "state", "status", "online", "dodo", "dodo_code",
                  "visitors", "visitor_count", "queue_count", "mash_b", "screen_on"):
            if k in d and d[k] not in (None, ""):
                bits.append(f"{k}={d[k]}")
        self.status_lbl.config(text="Status: " + (", ".join(bits) or json.dumps(d)[:160]))

    def _on_key(self, event):
        if not self.keys_var.get() or isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox)):
            return
        k = event.keysym if len(event.keysym) > 1 else event.keysym.lower()
        if k in KEYMAP:
            self._press(KEYMAP[k])
        elif k in STICK_KEYS:
            self._walk(*STICK_KEYS[k])

    def _log(self, text: str, ok: bool | None):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", "info" if ok is None else ("ok" if ok else "err"))
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 500:
            self.log.delete("1.0", f"{lines - 500}.0")
        self.log.see("end")
        self.log.config(state="disabled")


def _int(var: tk.IntVar, default: int) -> int:
    try:
        return max(1, int(var.get()))
    except (tk.TclError, ValueError):
        return default


def main():
    env = load_env()
    ap = argparse.ArgumentParser(description="Remote controller for a Chopaeng SysBot island.")
    ap.add_argument("--island", default="Hiraya", help="Island name (default: Hiraya)")
    ap.add_argument("--url", help="Direct SysBot base URL, e.g. http://127.0.0.1:8201 (island picker then only changes labels)")
    ap.add_argument("--template", help="Base URL template with {island}, e.g. http://127.0.0.1:8200/islands/{island}")
    ap.add_argument("--key", help="Bridge API key (default: SYSBOT_API_KEY from .env)")
    args = ap.parse_args()

    template = (args.url or args.template or env.get("SYSBOT_URL_TEMPLATE")
                or "http://127.0.0.1:8200/islands/{island}")
    island = next((i for i in ALL_ISLANDS if i.lower() == args.island.lower()), None)
    if not island:
        ap.error(f"unknown island {args.island!r}")
    bot = SysBot(template, args.key or env.get("SYSBOT_API_KEY", ""),
                 env.get("CF_ACCESS_CLIENT_ID", ""), env.get("CF_ACCESS_CLIENT_SECRET", ""))
    bot.island = island
    App(bot, island).mainloop()


if __name__ == "__main__":
    main()
