"""
File Listing Module

Unauthenticated directory listing API rooted at C:\\.

WARNING: This blueprint exposes the host filesystem over HTTP with NO
authentication. Anyone who can reach the Flask server can enumerate every
directory on C:\\ (and, via the ``path`` parameter, any absolute path). Only
mount this on a server that is bound to localhost or otherwise firewalled off
from untrusted networks.
"""

from __future__ import annotations

import logging
import os
import string
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request, send_file

logger = logging.getLogger("FilesAPI")

files_bp = Blueprint("files", __name__, url_prefix="/api/files")

# Root the listing at the C:\ drive by default.
DEFAULT_ROOT = "C:\\"


def _list_drives() -> list[dict]:
    """Return the available Windows drive roots (C:\\, D:\\, ...)."""
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(
                {"name": root, "path": root, "type": "dir", "size": None, "modified": None}
            )
    return drives


def _entry(base: str, name: str) -> dict:
    """Build a JSON-serializable descriptor for a single directory entry."""
    full = os.path.join(base, name)
    info: dict = {"name": name, "path": full}
    try:
        st = os.stat(full)
        is_dir = os.path.isdir(full)
        info["type"] = "dir" if is_dir else "file"
        info["size"] = None if is_dir else st.st_size
        info["modified"] = datetime.fromtimestamp(
            st.st_mtime, tz=timezone.utc
        ).isoformat()
    except (OSError, ValueError):
        # Permission denied, broken symlink, locked file, etc. Still list the
        # name so the caller knows it exists.
        info["type"] = "unknown"
        info["size"] = None
        info["modified"] = None
    return info


@files_bp.route("/drives", methods=["GET"])
def list_drives():
    """List available drive roots ("This PC")."""
    drives = _list_drives()
    return jsonify(
        {"path": "", "parent": None, "count": len(drives), "entries": drives, "total_size": 0}
    )


@files_bp.route("/", methods=["GET"])
@files_bp.route("", methods=["GET"])
def list_directory():
    """List the contents of a directory.

    Query params:
        path:   absolute directory to list. Defaults to ``C:\\``. An empty
                path returns the list of drives ("This PC").
        filter: optional case-insensitive substring to filter entry names.
    """
    raw = request.args.get("path", DEFAULT_ROOT)
    if raw is not None and raw.strip() == "":
        return list_drives()

    target = os.path.abspath(raw or DEFAULT_ROOT)

    if not os.path.exists(target):
        return jsonify({"error": "not_found", "path": target}), 404
    if not os.path.isdir(target):
        return jsonify({"error": "not_a_directory", "path": target}), 400

    try:
        names = sorted(os.listdir(target), key=str.lower)
    except PermissionError:
        return jsonify({"error": "permission_denied", "path": target}), 403
    except OSError as exc:
        logger.warning("Failed to list %s: %s", target, exc)
        return jsonify({"error": "io_error", "path": target, "detail": str(exc)}), 500

    needle = (request.args.get("filter") or "").lower()
    if needle:
        names = [n for n in names if needle in n.lower()]

    entries = [_entry(target, name) for name in names]
    total_size = sum(e["size"] or 0 for e in entries if e["type"] == "file")
    stripped = target.rstrip("\\/")
    if len(stripped) == 2 and stripped[1] == ":":  # drive root like "C:"
        parent = ""  # parent is the drive list ("This PC")
    else:
        parent = os.path.dirname(stripped) or ""

    return jsonify(
        {
            "path": target,
            "parent": parent,
            "count": len(entries),
            "total_size": total_size,
            "entries": entries,
        }
    )


@files_bp.route("/download", methods=["GET"])
def download_file():
    """Stream a single file to the client as a download.

    Query params:
        path: absolute path to the file.
    """
    target = request.args.get("path", "") or ""
    target = os.path.abspath(target)

    if not os.path.exists(target):
        return jsonify({"error": "not_found", "path": target}), 404
    if os.path.isdir(target):
        return jsonify({"error": "is_a_directory", "path": target}), 400

    try:
        # conditional=True enables HTTP Range requests so large downloads can
        # be resumed / seeked.
        return send_file(
            target,
            as_attachment=True,
            download_name=os.path.basename(target),
            conditional=True,
        )
    except PermissionError:
        return jsonify({"error": "permission_denied", "path": target}), 403
    except OSError as exc:
        logger.warning("Failed to send %s: %s", target, exc)
        return jsonify({"error": "io_error", "path": target, "detail": str(exc)}), 500


@files_bp.route("/view", methods=["GET"])
def view_file():
    """Serve a single file inline (open in the browser instead of downloading).

    Query params:
        path: absolute path to the file.
    """
    target = os.path.abspath(request.args.get("path", "") or "")

    if not os.path.exists(target):
        return jsonify({"error": "not_found", "path": target}), 404
    if os.path.isdir(target):
        return jsonify({"error": "is_a_directory", "path": target}), 400

    try:
        return send_file(target, as_attachment=False, conditional=True)
    except PermissionError:
        return jsonify({"error": "permission_denied", "path": target}), 403
    except OSError as exc:
        logger.warning("Failed to view %s: %s", target, exc)
        return jsonify({"error": "io_error", "path": target, "detail": str(exc)}), 500


@files_bp.route("/ui", methods=["GET"])
def browser_ui():
    """Serve a minimal single-page file browser."""
    return Response(_UI_HTML, mimetype="text/html")


_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File Browser</title>
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --fg: #e6e9ef;
    --muted: #8b93a7; --accent: #5b9dff; --row: #1b1f27;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 system-ui, -apple-system, Segoe UI, sans-serif;
         background: var(--bg); color: var(--fg); }
  header { padding: 14px 16px; background: var(--panel); border-bottom: 1px solid var(--border);
           position: sticky; top: 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; }
  .path-bar { flex: 1 1 320px; display: flex; gap: 8px; min-width: 240px; }
  input[type=text] { flex: 1; background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 7px; padding: 8px 10px; font: inherit; }
  button { background: var(--row); color: var(--fg); border: 1px solid var(--border);
    border-radius: 7px; padding: 8px 12px; cursor: pointer; font: inherit; }
  button:hover { border-color: var(--accent); }
  main { padding: 12px 16px; }
  .crumbs { color: var(--muted); margin-bottom: 10px; word-break: break-all; }
  .crumbs a { color: var(--accent); text-decoration: none; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--fg); }
  tr.row:hover { background: var(--row); }
  td.name { word-break: break-all; }
  a.entry { color: var(--fg); text-decoration: none; cursor: pointer; }
  a.entry:hover { color: var(--accent); }
  .ico { display: inline-block; width: 18px; text-align: center; margin-right: 6px; }
  .size, .mod { color: var(--muted); white-space: nowrap; }
  .dl { color: var(--accent); text-decoration: none; }
  .msg { color: #ff7a7a; padding: 10px 0; }
  .muted { color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>📁 File Browser</h1>
  <div class="path-bar">
    <button id="drivesBtn" title="This PC / drives">💽</button>
    <input id="pathInput" type="text" placeholder="C:\" spellcheck="false">
    <button id="goBtn">Go</button>
    <button id="upBtn" title="Parent folder">Up</button>
    <input id="filterInput" type="text" placeholder="filter…" spellcheck="false" style="flex:0 1 150px;min-width:110px">
  </div>
</header>
<main>
  <div class="crumbs" id="crumbs"></div>
  <div class="muted" id="summary" style="margin-bottom:8px"></div>
  <div id="content"></div>
</main>
<script>
const API = location.pathname.replace(/\/ui$/, "");
const pathInput = document.getElementById("pathInput");
const filterInput = document.getElementById("filterInput");
const crumbs = document.getElementById("crumbs");
const summary = document.getElementById("summary");
const content = document.getElementById("content");

let current = null;              // last loaded payload
let sort = { key: "name", dir: 1 };

function fmtSize(n) {
  if (n == null) return "";
  const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
function fmtDate(s) { return s ? new Date(s).toLocaleString() : ""; }

async function load(path) {
  content.innerHTML = '<p class="muted">Loading…</p>';
  summary.textContent = "";
  const url = API + "/?path=" + encodeURIComponent(path == null ? "" : path);
  let data;
  try {
    const r = await fetch(url);
    data = await r.json();
    if (!r.ok) throw new Error((data && data.error) || r.status);
  } catch (e) {
    content.innerHTML = '<p class="msg">Error: ' + e.message + '</p>';
    return;
  }
  current = data;
  pathInput.value = data.path;
  history.replaceState(null, "", "?path=" + encodeURIComponent(data.path));
  renderCrumbs(data.path);
  render();
}

function renderCrumbs(p) {
  if (!p) { crumbs.innerHTML = '<span class="muted">This PC</span>'; return; }
  const sep = p.includes("\\") ? "\\" : "/";
  const parts = p.split(/[\\/]/).filter(Boolean);
  let acc = "";
  const head = '<a data-path="">This PC</a>';
  const links = parts.map((part, i) => {
    acc += part + sep;
    return '<a data-path="' + acc.replace(/"/g, "&quot;") + '">' + escapeHtml(part) + '</a>';
  });
  crumbs.innerHTML = [head].concat(links).join(" " + sep + " ");
  crumbs.querySelectorAll("a").forEach(a =>
    a.onclick = () => load(a.dataset.path));
}

function render() {
  if (!current) return;
  const needle = filterInput.value.toLowerCase();
  let entries = current.entries.slice();
  if (needle) entries = entries.filter(e => e.name.toLowerCase().includes(needle));

  // Folders always before files, then by the active sort key.
  const k = sort.key, d = sort.dir;
  entries.sort((a, b) => {
    const ad = a.type === "dir", bd = b.type === "dir";
    if (ad !== bd) return ad ? -1 : 1;
    let av, bv;
    if (k === "size") { av = a.size || 0; bv = b.size || 0; }
    else if (k === "modified") { av = a.modified || ""; bv = b.modified || ""; }
    else { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
    return av < bv ? -d : av > bv ? d : 0;
  });

  const totalFiles = current.entries.filter(e => e.type !== "dir").length;
  const totalDirs = current.entries.length - totalFiles;
  summary.textContent =
    totalDirs + " folder" + (totalDirs === 1 ? "" : "s") + " · " +
    totalFiles + " file" + (totalFiles === 1 ? "" : "s") +
    (current.total_size ? " · " + fmtSize(current.total_size) : "") +
    (needle ? "  (showing " + entries.length + ")" : "");

  if (!entries.length) {
    content.innerHTML = '<p class="muted">' +
      (needle ? "No entries match the filter." : "Empty folder.") + '</p>';
    return;
  }

  const ind = key => sort.key === key ? (sort.dir > 0 ? " ▲" : " ▼") : "";
  const rows = entries.map(e => {
    const icon = e.type === "dir" ? "📂" : (e.type === "unknown" ? "🔒" : "📄");
    const name = e.type === "dir"
      ? '<a class="entry" data-dir="' + e.path.replace(/"/g,"&quot;") + '">' + escapeHtml(e.name) + '</a>'
      : escapeHtml(e.name);
    let actions = "";
    if (e.type === "file") {
      const q = "?path=" + encodeURIComponent(e.path);
      actions =
        '<a class="dl" target="_blank" href="' + API + '/view' + q + '">view</a>' +
        ' &nbsp; ' +
        '<a class="dl" href="' + API + '/download' + q + '">download</a>';
    }
    return '<tr class="row"><td class="name"><span class="ico">' + icon + '</span>' + name +
      '</td><td class="size">' + fmtSize(e.size) +
      '</td><td class="mod">' + fmtDate(e.modified) +
      '</td><td>' + actions + '</td></tr>';
  }).join("");

  content.innerHTML =
    '<table><thead><tr>' +
      '<th class="sortable" data-k="name">Name' + ind("name") + '</th>' +
      '<th class="sortable" data-k="size">Size' + ind("size") + '</th>' +
      '<th class="sortable" data-k="modified">Modified' + ind("modified") + '</th>' +
      '<th></th></tr></thead><tbody>' + rows + '</tbody></table>';

  content.querySelectorAll("a[data-dir]").forEach(a =>
    a.onclick = () => load(a.dataset.dir));
  content.querySelectorAll("th.sortable").forEach(th =>
    th.onclick = () => {
      const key = th.dataset.k;
      sort.dir = sort.key === key ? -sort.dir : 1;
      sort.key = key;
      render();
    });
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c =>
    ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

document.getElementById("goBtn").onclick = () => load(pathInput.value);
document.getElementById("drivesBtn").onclick = () => load("");
document.getElementById("upBtn").onclick = () => {
  const p = (pathInput.value || "").replace(/[\\/]+$/, "");
  if (!p || /^[A-Za-z]:$/.test(p)) { load(""); return; }   // drive root -> This PC
  const parent = p.replace(/[\\/][^\\/]*$/, "");
  load(parent || "");
};
pathInput.addEventListener("keydown", e => { if (e.key === "Enter") load(pathInput.value); });
filterInput.addEventListener("input", render);

const start = new URLSearchParams(location.search).get("path");
load(start || "");
</script>
</body>
</html>"""
