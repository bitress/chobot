"""
File Manager API (public, no authentication)

Browse, download, upload and delete files inside a fixed set of named root folders.
Clients never send absolute paths: they pick a root by name and a path relative to it,
and every resolved path is checked to stay inside that root (symlinks included).

Routes (NO authentication: anyone who can reach the API can use them, so only list
folders in FILE_ROOTS that are safe to expose, and FILE_ROOTS_WRITABLE ones are
publicly writable):
  GET    /files                              -> list configured roots
  GET    /files?root=<name>&path=sub/dir     -> list a directory
  GET    /files/download?root=<name>&path=…  -> stream a file (supports Range)
  PUT    /files/upload?root=<name>&path=…    -> upload raw request body (writable roots only)
                                                 add &overwrite=1 to replace an existing file
  DELETE /files?root=<name>&path=…           -> delete a file or empty folder (writable roots only)

Configuration (.env):
  FILE_ROOTS=logs=C:\\SysBot\\logs;nhi=C:\\SysBot\\villagers
  FILE_ROOTS_WRITABLE=nhi            # comma-separated root names that allow upload/delete
  FILE_UPLOAD_MAX_MB=100
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from functools import wraps
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

logger = logging.getLogger("FileManager")

file_manager_bp = Blueprint("file_manager", __name__, url_prefix="/files")

_CHUNK = 1024 * 1024


class FileApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ── Configuration ────────────────────────────────────────────────────────────

def _parse_roots(raw: str) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for entry in (raw or "").split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, path = entry.split("=", 1)
        name, path = name.strip(), path.strip().strip('"')
        if not name or not path:
            continue
        try:
            roots[name] = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            logger.warning("FILE_ROOTS: skipping %r, folder %r does not exist", name, path)
    return roots


def _roots() -> dict[str, Path]:
    return _parse_roots(os.getenv("FILE_ROOTS", ""))


def _writable_roots() -> set[str]:
    return {n.strip() for n in os.getenv("FILE_ROOTS_WRITABLE", "").split(",") if n.strip()}


def _upload_limit_bytes() -> int:
    try:
        mb = float(os.getenv("FILE_UPLOAD_MAX_MB", "100"))
    except ValueError:
        mb = 100
    return max(int(mb * 1024 * 1024), 0)


# ── Error handling ───────────────────────────────────────────────────────────

def _handle_errors(fn):
    """Public endpoints (no auth). Turns FileApiError into a JSON error response."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FileApiError as exc:
            return jsonify({"success": False, "error": exc.message}), exc.status

    return wrapper


def _audit(action: str, root: str, rel: str, **details) -> None:
    try:
        from api.flask_api import _record_api_audit_event

        _record_api_audit_event(action, f"{root}:{rel}", details or None)
    except Exception as exc:  # audit is best-effort
        logger.debug("File audit failed: %s", exc)


# ── Path resolution ──────────────────────────────────────────────────────────

def _is_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve(root_name: str | None, rel: str | None, *, must_exist: bool = True) -> tuple[Path, Path, str]:
    """Return (root, target, normalized_rel) or raise FileApiError."""
    roots = _roots()
    if not roots:
        raise FileApiError(503, "File API is not configured (set FILE_ROOTS).")
    if not root_name or root_name not in roots:
        raise FileApiError(404, "Unknown root.")
    root = roots[root_name]

    rel = (rel or "").replace("\\", "/").strip()
    if "\x00" in rel:
        raise FileApiError(400, "Invalid path.")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or (parts and (":" in parts[0])):
        raise FileApiError(400, "Invalid path.")

    target = root.joinpath(*parts) if parts else root
    try:
        resolved = target.resolve(strict=must_exist)
    except FileNotFoundError:
        raise FileApiError(404, "Not found.")
    except (OSError, RuntimeError):
        raise FileApiError(400, "Invalid path.")

    if not _is_within(root, resolved):
        raise FileApiError(403, "Path is outside the root.")
    return root, resolved, "/".join(parts)


def _entry(root: Path, p: Path) -> dict:
    st = p.stat()
    is_dir = p.is_dir()
    return {
        "name": p.name,
        "path": p.relative_to(root).as_posix(),
        "is_dir": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": int(st.st_mtime),
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@file_manager_bp.route("", methods=["GET"])
@_handle_errors
def list_files():
    root_name = request.args.get("root")
    if not root_name:
        writable = _writable_roots()
        return jsonify({
            "success": True,
            "roots": [{"name": n, "writable": n in writable} for n in sorted(_roots())],
        })

    root, target, rel = _resolve(root_name, request.args.get("path"))
    if not target.is_dir():
        return jsonify({"success": True, "root": root_name, "path": rel, "entry": _entry(root, target)})

    entries = []
    for child in target.iterdir():
        try:
            resolved = child.resolve()
            if not _is_within(root, resolved):
                continue  # hide symlinks that point outside the root
            entries.append(_entry(root, child))
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return jsonify({"success": True, "root": root_name, "path": rel, "entries": entries})


@file_manager_bp.route("/download", methods=["GET"])
@_handle_errors
def download_file():
    root_name = request.args.get("root")
    _root, target, rel = _resolve(root_name, request.args.get("path"))
    if not target.is_file():
        raise FileApiError(400, "Not a file.")
    _audit("file_download", root_name, rel)
    return send_file(target, as_attachment=True, download_name=target.name, conditional=True)


def _require_writable(root_name: str | None) -> None:
    if root_name not in _writable_roots():
        raise FileApiError(403, "This root is read-only.")


@file_manager_bp.route("/upload", methods=["PUT"])
@_handle_errors
def upload_file():
    root_name = request.args.get("root")
    _require_writable(root_name)
    root, target, rel = _resolve(root_name, request.args.get("path"), must_exist=False)
    if not rel:
        raise FileApiError(400, "A file path is required.")
    if target.is_dir():
        raise FileApiError(409, "A folder exists at that path.")
    overwrite = request.args.get("overwrite", "").lower() in ("1", "true", "yes")
    if target.exists() and not overwrite:
        raise FileApiError(409, "File exists (pass overwrite=1 to replace).")

    limit = _upload_limit_bytes()
    if request.content_length is not None and request.content_length > limit:
        raise FileApiError(413, f"Upload exceeds {limit} bytes.")

    target.parent.mkdir(parents=True, exist_ok=True)
    if not _is_within(root, target.parent.resolve()):
        raise FileApiError(403, "Path is outside the root.")

    fd, tmp_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = request.stream.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise FileApiError(413, f"Upload exceeds {limit} bytes.")
                out.write(chunk)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    _audit("file_upload", root_name, rel, size=written, overwrite=overwrite)
    return jsonify({"success": True, "entry": _entry(root, target)}), 201


@file_manager_bp.route("", methods=["DELETE"])
@_handle_errors
def delete_file():
    root_name = request.args.get("root")
    _require_writable(root_name)
    root, target, rel = _resolve(root_name, request.args.get("path"))
    if target == root:
        raise FileApiError(400, "Cannot delete the root.")
    if target.is_dir():
        try:
            target.rmdir()
        except OSError:
            raise FileApiError(409, "Folder is not empty.")
    else:
        target.unlink()
    _audit("file_delete", root_name, rel)
    return jsonify({"success": True, "deleted": rel})

