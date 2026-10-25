"""
Unified Account & Just-In-Time (JIT) Account Provisioning Module
================================================================
Handles:
- JIT Account Provisioning on OAuth login (Patreon, YouTube, Twitch)
- Cross-provider Account Linking & Identity Unification
- Auto-linking by verified email or active session token
- Aggregating entitlements and roles across multiple connected accounts
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

import bcrypt

from utils.config import Config
from utils.database import connect_db

logger = logging.getLogger("UnifiedAuth")

ROLE_ID_TO_TIER_NAME: Dict[str, str] = {
    str(getattr(Config, "ROLE_CHOSOUP", 1031974843656192080)): "ChoSoup",
    str(getattr(Config, "ROLE_Y_CHOSOUP", 1048323845771247681)): "y ChoSoup",
    str(getattr(Config, "ROLE_CHOTATO_CLUB", 898373041799696454)): "ChoTato Club",
    str(getattr(Config, "ROLE_Y_CHOTATO_CLUB", 1048323845771247679)): "y ChoTato",
    str(getattr(Config, "ROLE_CHOCOLATE", 1039516557405081601)): "ChoColate",
    str(getattr(Config, "ROLE_Y_CHOCOLATE", 1048323845771247680)): "y ChoColate",
    str(getattr(Config, "ROLE_CHOFRIES", 1031974954113179729)): "ChoFries",
    str(getattr(Config, "ROLE_SERVER_BOOSTER", 760256226448638006)): "Server Booster",
}
TIER_NAME_TO_ROLE_ID: Dict[str, str] = {v: k for k, v in ROLE_ID_TO_TIER_NAME.items()}


def expand_roles(roles: List[Any]) -> List[str]:
    """Ensure roles include both canonical tier name and configured Discord role ID."""
    result: set[str] = set()
    for r in roles:
        s = str(r).strip()
        if not s:
            continue
        result.add(s)
        if s in ROLE_ID_TO_TIER_NAME:
            result.add(ROLE_ID_TO_TIER_NAME[s])
        elif s in TIER_NAME_TO_ROLE_ID:
            result.add(TIER_NAME_TO_ROLE_ID[s])
    return list(result)



def _ensure_unified_auth_tables(conn) -> None:
    """Ensure persistent tables exist for unified user accounts, native accounts, and provider links."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chopaeng_users (
            id VARCHAR(64) PRIMARY KEY,
            email VARCHAR(255) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            username VARCHAR(64) NOT NULL,
            avatar VARCHAR(512) DEFAULT '',
            is_admin BOOLEAN DEFAULT 0,
            is_mod BOOLEAN DEFAULT 0,
            is_verified BOOLEAN DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_login_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS unified_accounts (
            unified_user_id VARCHAR(128) PRIMARY KEY,
            primary_username VARCHAR(255) NOT NULL,
            primary_avatar VARCHAR(512),
            primary_email VARCHAR(255),
            primary_provider VARCHAR(32) NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_provider_links (
            provider VARCHAR(32) NOT NULL,
            provider_user_id VARCHAR(128) NOT NULL,
            unified_user_id VARCHAR(128) NOT NULL,
            email VARCHAR(255),
            username VARCHAR(255),
            avatar VARCHAR(512),
            roles_json TEXT,
            sub_tier VARCHAR(64),
            raw_profile_json TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (provider, provider_user_id)
        )
        """
    )
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS ix_chopaeng_users_email ON chopaeng_users (email)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_upl_unified_uid ON user_provider_links (unified_user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_upl_email ON user_provider_links (email)")
    except Exception:
        pass


def hash_password(password: str) -> str:
    """Hash password using bcrypt with standard work factor."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against bcrypt hash."""
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def register_native_user(
    email: str,
    password: str,
    username: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Register a primary native ChoPaeng account with email & password.
    Returns (user_payload, None) on success, or (None, error_msg) on failure.
    """
    if not email or "@" not in email:
        return None, "A valid email address is required."
    clean_email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean_email):
        return None, "Please provide a valid email format."

    if not password or len(password) < 6:
        return None, "Password must be at least 6 characters long."

    clean_username = (username or "").strip()
    if not clean_username:
        clean_username = clean_email.split("@")[0]

    now = int(time.time())
    conn = connect_db()
    try:
        _ensure_unified_auth_tables(conn)

        # Check if email already registered with password
        existing = conn.execute(
            "SELECT * FROM chopaeng_users WHERE email = ?",
            (clean_email,),
        ).fetchone()

        if existing:
            if existing["password_hash"]:
                return None, "An account with this email already exists. Please log in."
            # Account was auto-provisioned via OAuth without password; set credentials now
            user_id = str(existing["id"])
            pw_hash = hash_password(password)
            conn.execute(
                """
                UPDATE chopaeng_users
                SET password_hash = ?, username = ?, updated_at = ?, last_login_at = ?
                WHERE id = ?
                """,
                (pw_hash, clean_username or existing["username"], now, now, user_id),
            )
            conn.commit()
            return get_unified_account_by_id(user_id), None

        # Check if an existing unified_accounts row matches this email from prior OAuth
        prior_unified = conn.execute(
            "SELECT unified_user_id FROM user_provider_links WHERE email = ? LIMIT 1",
            (clean_email,),
        ).fetchone()

        if prior_unified:
            user_id = str(prior_unified["unified_user_id"])
        else:
            user_id = f"cp_usr_{secrets.token_hex(8)}"

        pw_hash = hash_password(password)
        conn.execute(
            """
            INSERT INTO chopaeng_users
            (id, email, password_hash, username, avatar, is_admin, is_mod, is_verified, created_at, updated_at, last_login_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, clean_email, pw_hash, clean_username, "", 0, 0, 1, now, now, now),
        )

        conn.execute(
            """
            INSERT INTO unified_accounts
            (unified_user_id, primary_username, primary_avatar, primary_email, primary_provider, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unified_user_id) DO UPDATE SET
                primary_username = excluded.primary_username,
                primary_email = excluded.primary_email,
                updated_at = excluded.updated_at
            """,
            (user_id, clean_username, "", clean_email, "native", now, now),
        )

        # Also add website native provider link
        conn.execute(
            """
            INSERT INTO user_provider_links
            (provider, provider_user_id, unified_user_id, email, username, avatar, roles_json, sub_tier, raw_profile_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_user_id) DO UPDATE SET
                email = excluded.email,
                username = excluded.username,
                updated_at = excluded.updated_at
            """,
            ("website", user_id, user_id, clean_email, clean_username, "", "[]", "", "{}", now, now),
        )
        conn.commit()

        return get_unified_account_by_id(user_id), None
    finally:
        conn.close()


def authenticate_native_user(
    email: str,
    password: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Authenticate a native ChoPaeng account with email & password.
    Returns (user_payload, None) on success, or (None, error_msg) on failure.
    """
    if not email or not password:
        return None, "Email and password are required."

    clean_email = email.strip().lower()
    conn = connect_db()
    try:
        _ensure_unified_auth_tables(conn)
        user_row = conn.execute(
            "SELECT * FROM chopaeng_users WHERE email = ?",
            (clean_email,),
        ).fetchone()

        if not user_row:
            return None, "Invalid email or password."

        if not user_row["password_hash"]:
            return None, "This account was registered via social login. Please sign in with your connected account or set a password in settings."

        if not verify_password(password, user_row["password_hash"]):
            return None, "Invalid email or password."

        now = int(time.time())
        conn.execute("UPDATE chopaeng_users SET last_login_at = ? WHERE id = ?", (now, user_row["id"]))
        conn.commit()

        payload = get_unified_account_by_id(user_row["id"])
        if payload:
            if user_row.get("is_admin"):
                payload["is_admin"] = True
            if user_row.get("is_mod"):
                payload["is_mod"] = True
        return payload, None
    finally:
        conn.close()


def set_native_password(
    user_id: str,
    new_password: str,
    old_password: Optional[str] = None,
) -> Tuple[bool, str]:
    """Set or change password for a user."""
    if not user_id:
        return False, "User ID is required."
    if not new_password or len(new_password) < 6:
        return False, "Password must be at least 6 characters long."

    conn = connect_db()
    try:
        _ensure_unified_auth_tables(conn)
        row = conn.execute("SELECT * FROM chopaeng_users WHERE id = ?", (user_id,)).fetchone()
        now = int(time.time())
        if not row:
            # Check unified_accounts
            u_row = conn.execute("SELECT * FROM unified_accounts WHERE unified_user_id = ?", (user_id,)).fetchone()
            if not u_row:
                return False, "User account not found."
            email = u_row["primary_email"] or f"{user_id}@chopaeng.com"
            uname = u_row["primary_username"] or "ChoResident"
            pw_hash = hash_password(new_password)
            conn.execute(
                """
                INSERT INTO chopaeng_users
                (id, email, password_hash, username, avatar, is_admin, is_mod, is_verified, created_at, updated_at, last_login_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, email, pw_hash, uname, u_row["primary_avatar"] or "", 0, 0, 1, now, now, now),
            )
            conn.commit()
            return True, "Password set successfully."

        if row["password_hash"] and old_password:
            if not verify_password(old_password, row["password_hash"]):
                return False, "Current password is incorrect."
        elif row["password_hash"] and not old_password:
            return False, "Current password is required to change password."

        pw_hash = hash_password(new_password)
        conn.execute(
            "UPDATE chopaeng_users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (pw_hash, now, user_id),
        )
        conn.commit()
        return True, "Password updated successfully."
    finally:
        conn.close()


def get_unified_account_by_id(unified_user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve full unified account payload with all linked providers and aggregated roles."""
    if not unified_user_id:
        return None

    conn = connect_db()
    try:
        _ensure_unified_auth_tables(conn)
        account_row = conn.execute(
            "SELECT * FROM unified_accounts WHERE unified_user_id = ?",
            (unified_user_id,),
        ).fetchone()

        links = conn.execute(
            "SELECT * FROM user_provider_links WHERE unified_user_id = ?",
            (unified_user_id,),
        ).fetchall()

        if not account_row and not links:
            return None

        return _build_unified_payload(account_row, links, unified_user_id, conn=conn)
    finally:
        conn.close()


def find_unified_user_id(
    provider: str,
    provider_user_id: str,
    email: Optional[str] = None,
    active_user_id: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve which unified_user_id an OAuth identity belongs to.
    Resolution order:
      1. Explicit active session (user is linking an account while logged in)
      2. Existing link for (provider, provider_user_id)
      3. Existing link or native account matching the verified email
    """
    if active_user_id and active_user_id.strip():
        act_id = active_user_id.strip()
        conn = connect_db()
        try:
            _ensure_unified_auth_tables(conn)
            # 1a. Check if active_user_id matches a unified_user_id directly
            u_chk = conn.execute(
                "SELECT unified_user_id FROM unified_accounts WHERE unified_user_id = ?",
                (act_id,),
            ).fetchone()
            if u_chk:
                return str(u_chk["unified_user_id"])

            # 1b. Check if active_user_id is a provider_user_id (e.g. Discord snowflake)
            p_chk = conn.execute(
                "SELECT unified_user_id FROM user_provider_links WHERE provider_user_id = ? LIMIT 1",
                (act_id,),
            ).fetchone()
            if p_chk:
                return str(p_chk["unified_user_id"])

            try:
                cp_chk = conn.execute("SELECT id FROM chopaeng_users WHERE id = ? LIMIT 1", (act_id,)).fetchone()
                if cp_chk:
                    return str(cp_chk["id"])
            except Exception:
                pass

            return act_id
        finally:
            conn.close()

    conn = connect_db()
    try:
        _ensure_unified_auth_tables(conn)

        # 1. Direct link check
        row = conn.execute(
            "SELECT unified_user_id FROM user_provider_links WHERE provider = ? AND provider_user_id = ?",
            (provider.lower(), str(provider_user_id)),
        ).fetchone()
        if row:
            return str(row["unified_user_id"])

        # 2. Email auto-link check
        if email and email.strip():
            clean_email = email.strip().lower()
            try:
                native_row = conn.execute(
                    "SELECT id FROM chopaeng_users WHERE email = ? LIMIT 1",
                    (clean_email,),
                ).fetchone()
                if native_row:
                    logger.info(
                        "Auto-linking provider %s:%s to native Chopaeng account %s by matching email %s",
                        provider,
                        provider_user_id,
                        native_row["id"],
                        email,
                    )
                    return str(native_row["id"])
            except Exception:
                pass

            email_row = conn.execute(
                "SELECT unified_user_id FROM user_provider_links WHERE email = ? AND email != '' LIMIT 1",
                (clean_email,),
            ).fetchone()
            if email_row:
                logger.info(
                    "Auto-linking provider %s:%s to unified account %s by matching email %s",
                    provider,
                    provider_user_id,
                    email_row["unified_user_id"],
                    email,
                )
                return str(email_row["unified_user_id"])

        return None
    finally:
        conn.close()


def provision_or_link_unified_account(
    provider: str,
    provider_user_id: str,
    username: str,
    avatar: str = "",
    email: Optional[str] = None,
    roles: Optional[List[str]] = None,
    sub_tier: Optional[str] = None,
    raw_profile: Optional[Dict[str, Any]] = None,
    active_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Just-In-Time (JIT) provisioning and account unification engine.
    - If account doesn't exist -> Provisions new unified account JIT.
    - If account exists (via linking, session, or email) -> Links provider to unified account JIT.
    - Merges entitlements and returns the aggregated user session dict.
    """
    provider = provider.lower()
    provider_user_id = str(provider_user_id).strip()
    clean_email = email.strip().lower() if email else ""
    roles = roles or []
    raw_profile = raw_profile or {}
    now = int(time.time())

    conn = connect_db()
    try:
        _ensure_unified_auth_tables(conn)

        # Resolve or generate unified_user_id
        unified_uid = find_unified_user_id(
            provider=provider,
            provider_user_id=provider_user_id,
            email=clean_email,
            active_user_id=active_user_id,
        )

        is_new_account = False
        if not unified_uid:
            is_new_account = True
            # For Discord accounts, use discord snowflake as unified_uid for backward compatibility
            if provider == "discord":
                unified_uid = provider_user_id
            else:
                unified_uid = f"u_{provider}_{provider_user_id}"

            logger.info("JIT Provisioning new unified account %s from %s:%s", unified_uid, provider, username)
            conn.execute(
                """
                INSERT INTO unified_accounts
                (unified_user_id, primary_username, primary_avatar, primary_email, primary_provider, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (unified_uid, username, avatar, clean_email, provider, now, now),
            )
        else:
            logger.info("JIT Linking provider %s:%s to unified account %s", provider, username, unified_uid)
            account_row_chk = conn.execute(
                "SELECT 1 FROM unified_accounts WHERE unified_user_id = ?",
                (unified_uid,),
            ).fetchone()

            is_native = False
            try:
                cp_chk = conn.execute("SELECT 1 FROM chopaeng_users WHERE id = ?", (unified_uid,)).fetchone()
                if cp_chk:
                    is_native = True
            except Exception:
                pass

            # Check if Discord is already linked to this unified account
            has_discord = conn.execute(
                "SELECT username, avatar, email FROM user_provider_links WHERE unified_user_id = ? AND provider = 'discord'",
                (unified_uid,),
            ).fetchone()

            if not account_row_chk:
                p_prov = "native" if is_native else ("discord" if (has_discord or provider == "discord") else provider)
                p_user = (has_discord["username"] if has_discord else username)
                p_av = (has_discord["avatar"] if has_discord else avatar)
                p_em = (has_discord["email"] if has_discord else clean_email)
                conn.execute(
                    """
                    INSERT INTO unified_accounts
                    (unified_user_id, primary_username, primary_avatar, primary_email, primary_provider, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (unified_uid, p_user, p_av, p_em, p_prov, now, now),
                )
            elif is_native:
                conn.execute(
                    """
                    UPDATE unified_accounts
                    SET updated_at = ?,
                        primary_provider = 'native',
                        primary_avatar = CASE WHEN (primary_avatar IS NULL OR primary_avatar = '') THEN ? ELSE primary_avatar END
                    WHERE unified_user_id = ?
                    """,
                    (now, avatar, unified_uid),
                )
            elif has_discord or provider == "discord":
                # Discord is main! Ensure primary_provider remains 'discord' and Discord username/avatar take priority
                disc_name = has_discord["username"] if has_discord else (username if provider == "discord" else "")
                disc_av = has_discord["avatar"] if has_discord else (avatar if provider == "discord" else "")
                conn.execute(
                    """
                    UPDATE unified_accounts
                    SET updated_at = ?,
                        primary_provider = 'discord',
                        primary_username = CASE WHEN (? != '') THEN ? ELSE primary_username END,
                        primary_avatar = CASE WHEN (? != '') THEN ? ELSE primary_avatar END,
                        primary_email  = CASE WHEN (primary_email IS NULL OR primary_email = '') THEN ? ELSE primary_email END
                    WHERE unified_user_id = ?
                    """,
                    (now, disc_name, disc_name, disc_av, disc_av, clean_email, unified_uid),
                )
            else:
                conn.execute(
                    """
                    UPDATE unified_accounts
                    SET updated_at = ?,
                        primary_avatar = CASE WHEN (primary_avatar IS NULL OR primary_avatar = '') THEN ? ELSE primary_avatar END,
                        primary_email  = CASE WHEN (primary_email IS NULL OR primary_email = '') THEN ? ELSE primary_email END
                    WHERE unified_user_id = ?
                    """,
                    (now, avatar, clean_email, unified_uid),
                )

        # Upsert user_provider_links
        roles_json = json.dumps(expand_roles(roles), separators=(",", ":"))
        raw_json = json.dumps(raw_profile, separators=(",", ":"))

        conn.execute(
            """
            INSERT INTO user_provider_links
            (provider, provider_user_id, unified_user_id, email, username, avatar, roles_json, sub_tier, raw_profile_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_user_id) DO UPDATE SET
                unified_user_id = excluded.unified_user_id,
                email           = excluded.email,
                username        = excluded.username,
                avatar          = excluded.avatar,
                roles_json      = excluded.roles_json,
                sub_tier        = excluded.sub_tier,
                raw_profile_json = excluded.raw_profile_json,
                updated_at      = excluded.updated_at
            """,
            (
                provider,
                provider_user_id,
                unified_uid,
                clean_email,
                username,
                avatar,
                roles_json,
                sub_tier or "",
                raw_json,
                now,
                now,
            ),
        )
        conn.commit()

        # Query all linked providers for this unified user to aggregate session
        account_row = conn.execute(
            "SELECT * FROM unified_accounts WHERE unified_user_id = ?",
            (unified_uid,),
        ).fetchone()

        links = conn.execute(
            "SELECT * FROM user_provider_links WHERE unified_user_id = ?",
            (unified_uid,),
        ).fetchall()

        return _build_unified_payload(account_row, links, unified_uid, conn=conn)
    finally:
        conn.close()


def unlink_provider_from_unified_account(unified_user_id: str, provider: str) -> Optional[Dict[str, Any]]:
    """Unlink a specific provider from the unified account."""
    if not unified_user_id or not provider:
        return None

    provider = provider.lower()
    if provider == "discord":
        logger.warning("Attempted to unlink primary provider 'discord' from unified account %s; rejected", unified_user_id)
        return None
    conn = connect_db()
    try:
        _ensure_unified_auth_tables(conn)
        conn.execute(
            "DELETE FROM user_provider_links WHERE unified_user_id = ? AND provider = ?",
            (unified_user_id, provider),
        )
        conn.commit()

        account_row = conn.execute(
            "SELECT * FROM unified_accounts WHERE unified_user_id = ?",
            (unified_user_id,),
        ).fetchone()

        links = conn.execute(
            "SELECT * FROM user_provider_links WHERE unified_user_id = ?",
            (unified_user_id,),
        ).fetchall()

        if not account_row and not links:
            return None

        return _build_unified_payload(account_row, links, unified_user_id, conn=conn)
    finally:
        conn.close()


def _build_unified_payload(account_row: Any, links: List[Any], unified_uid: str, conn: Any = None) -> Dict[str, Any]:
    """Helper to aggregate all roles, connected providers, and metadata into a unified user dict."""
    aggregated_roles: List[str] = []
    connected_accounts: Dict[str, Any] = {}
    connected_providers: List[str] = []

    primary_name = account_row["primary_username"] if account_row else ""
    primary_avatar = account_row["primary_avatar"] if account_row else ""
    primary_provider = account_row["primary_provider"] if account_row else "unified"
    primary_email = (account_row["primary_email"] if account_row else "") or ""

    has_password = False
    is_admin = False
    is_mod = False

    close_conn = False
    db_conn = conn
    if db_conn is None:
        try:
            db_conn = connect_db()
            close_conn = True
        except Exception:
            db_conn = None

    if db_conn:
        try:
            cp_row = db_conn.execute(
                "SELECT email, password_hash, is_admin, is_mod, username, avatar FROM chopaeng_users WHERE id = ?",
                (unified_uid,),
            ).fetchone()
            if cp_row:
                has_password = bool(cp_row["password_hash"])
                is_admin = bool(cp_row["is_admin"])
                is_mod = bool(cp_row["is_mod"])
                primary_provider = "native"
                if cp_row["email"]:
                    primary_email = cp_row["email"]
                if cp_row["username"]:
                    primary_name = cp_row["username"]
                if cp_row["avatar"]:
                    primary_avatar = cp_row["avatar"]
        except Exception:
            pass
        finally:
            if close_conn:
                db_conn.close()

    highest_tier: Optional[str] = None
    tier_priority = {
        "ChoSoup": 3,
        "y ChoSoup": 3,
        "ChoColate": 2,
        "y ChoColate": 2,
        "ChoTato Club": 1,
        "ChoFries": 1,
        "y ChoTato": 1,
    }

    for link_item in links:
        link = dict(link_item) if not isinstance(link_item, dict) else link_item
        p_name = str(link.get("provider", "")).lower()
        connected_providers.append(p_name)
        link_roles = []
        try:
            link_roles = json.loads(link["roles_json"] or "[]")
        except Exception:
            link_roles = []

        aggregated_roles.extend(link_roles)

        sub_t = link.get("sub_tier") or (link_roles[0] if link_roles else None)
        if sub_t:
            cur_p = tier_priority.get(highest_tier, 0) if highest_tier else 0
            new_p = tier_priority.get(sub_t, 0)
            if new_p >= cur_p:
                highest_tier = sub_t

        connected_accounts[p_name] = {
            "id": link["provider_user_id"],
            "username": link["username"],
            "avatar": link["avatar"],
            "email": link["email"],
            "roles": link_roles,
            "sub_tier": sub_t,
            "linked_at": link["created_at"],
        }

        if not primary_name and link["username"]:
            primary_name = link["username"]
        if not primary_avatar and link["avatar"]:
            primary_avatar = link["avatar"]

    # Discord is the main provider! If Discord is connected, it takes priority over secondary platforms
    discord_acc = connected_accounts.get("discord")
    if discord_acc:
        if primary_provider != "native":
            primary_provider = "discord"
            if discord_acc.get("username"):
                primary_name = discord_acc["username"]
            if discord_acc.get("avatar"):
                primary_avatar = discord_acc["avatar"]

    # De-duplicate and expand roles
    unique_roles = expand_roles(aggregated_roles)

    try:
        from utils.discord_membership import _admin_role_ids
        if bool(set(str(r) for r in unique_roles) & _admin_role_ids()):
            is_admin = True
    except Exception:
        pass

    try:
        from utils.island_access import is_mod as _check_mod
        if _check_mod(unique_roles):
            is_mod = True
    except Exception:
        pass

    if is_admin:
        is_mod = True

    discord_id = (
        (discord_acc.get("id") if discord_acc else None)
        or (unified_uid if primary_provider == "discord" else None)
    )
    discord_name = (
        (discord_acc.get("username") if discord_acc else None)
        or primary_name
        or unified_uid
    )

    return {
        "user_id": unified_uid,
        "discord_id": discord_id,
        "username": primary_name or unified_uid,
        "discord_name": discord_name,
        "email": primary_email,
        "avatar": primary_avatar,
        "provider": primary_provider,
        "has_password": has_password,
        "roles": unique_roles,
        "sub_tier": highest_tier,
        "is_subscriber": bool(highest_tier or unique_roles),
        "is_admin": is_admin,
        "is_mod": is_mod,
        "connected_providers": list(dict.fromkeys(connected_providers)),
        "connected_accounts": connected_accounts,
        "auth_checked_at": int(time.time()),
    }


def get_user_candidate_ids(user: Optional[Dict[str, Any]]) -> List[str]:
    """
    Return all possible user identifiers for a given user payload:
    - Primary user_id (e.g. cp_usr_... or Discord snowflake)
    - id
    - discord_id
    - Connected account provider IDs (e.g. discord snowflake, twitch id, patreon id)
    - Provider-prefixed IDs (e.g. 'discord_123456789')
    """
    if not user or not isinstance(user, dict):
        return []

    cands: list[str] = []
    for k in ("user_id", "id", "discord_id"):
        val = user.get(k)
        if val:
            cands.append(str(val).strip())

    connected = user.get("connected_accounts") or {}
    if isinstance(connected, dict):
        for p_name, p_data in connected.items():
            uid = ""
            if isinstance(p_data, dict):
                uid = str(p_data.get("id") or "").strip()
            elif p_data:
                uid = str(p_data).strip()

            if uid:
                cands.append(uid)
                cands.append(f"{p_name}_{uid}")

    return list(dict.fromkeys([c for c in cands if c]))


def resolve_all_user_ids(user_or_id: Any) -> List[str]:
    """
    Resolve all linked user IDs (unified UID, discord_id, twitch_id, etc.)
    accepting either a user dict or a raw user_id string.
    """
    cands: list[str] = []
    if isinstance(user_or_id, dict):
        cands.extend(get_user_candidate_ids(user_or_id))
    elif user_or_id:
        cands.append(str(user_or_id).strip())

    cp_uids = [c for c in cands if c.startswith("cp_usr_")]
    if cp_uids:
        try:
            with connect_db() as conn:
                for cp_uid in cp_uids:
                    rows = conn.execute(
                        "SELECT provider, provider_user_id FROM user_provider_links WHERE unified_user_id = ?",
                        (cp_uid,),
                    ).fetchall()
                    for r in rows:
                        p_uid = str(r["provider_user_id"] or "").strip()
                        if p_uid:
                            cands.append(p_uid)
                            cands.append(f"{r['provider']}_{p_uid}")
        except Exception:
            pass
    elif cands and not cp_uids:
        try:
            with connect_db() as conn:
                for uid in list(cands):
                    rows = conn.execute(
                        "SELECT unified_user_id FROM user_provider_links WHERE provider_user_id = ?",
                        (uid,),
                    ).fetchall()
                    for r in rows:
                        u_uid = str(r["unified_user_id"] or "").strip()
                        if u_uid:
                            cands.append(u_uid)
        except Exception:
            pass

    return list(dict.fromkeys([c for c in cands if c]))


# Backward-compatible alias
provision_unified_account_jit = provision_or_link_unified_account


