"""
db.py — Encrypted SQLCipher database layer for Friez.

All data previously stored in JSON files lives here.
Requires: sqlcipher3
  pip install sqlcipher3 --break-system-packages

DB lives on Pi5 (192.168.30.80), mounted on Pi Zero via SSHFS:
  sshfs pi@192.168.30.80:/home/pi/Sky/frozy.db /home/pi/Sky/frozy.db

Encryption key loaded from env: FROZY_DB_KEY
"""

import os
import time
import datetime
from contextlib import contextmanager

# sqlcipher3 is a drop-in replacement for sqlite3 with encryption
try:
    import sqlcipher3 as sqlite3
    _ENCRYPTED = True
except ImportError:
    import sqlite3
    _ENCRYPTED = False
    print("[db] WARNING: sqlcipher3 not found — falling back to unencrypted SQLite.")

DB_PATH = os.getenv("FROZY_DB_PATH", "/home/pi/Sky/frozy.db")
DB_KEY  = os.getenv("FROZY_DB_KEY",  "")   # must be set in .env

if not DB_KEY and _ENCRYPTED:
    raise RuntimeError("FROZY_DB_KEY env var not set — refusing to open encrypted DB without a key.")

# ── Connection ─────────────────────────────────────────────────────────────────

# ── Write-through sync ────────────────────────────────────────────────────────
# Both Pis share the same DB file path. After every write, we push the DB to
# the remote Pi so neither side ever reads stale data.
#
# Pi Zero  → pushes to Pi5   (bot writes, dashboard reads)
# Pi5      → pushes to Pi Zero (dashboard writes economy/mod, bot needs to see it)
#
# Set FROZY_SYNC_REMOTE=pi@192.168.30.80:/home/pi/Sky/frozy.db on Pi Zero
# Set FROZY_SYNC_REMOTE=pi@192.168.30.11:/home/pi/Sky/frozy.db on Pi5
# Leave unset to disable (e.g. in dev).

import subprocess as _subprocess
import threading as _threading

_SYNC_REMOTE   = os.getenv("FROZY_SYNC_REMOTE", "")   # user@host:path
_sync_lock     = _threading.Lock()
_sync_pending  = _threading.Event()
_sync_thread   = None


def _id(v) -> str:
    """Normalize any Discord snowflake to str for DB storage."""
    return str(v)


def _sync_worker():
    """Background thread: coalesces rapid writes into one rsync per burst."""
    while True:
        _sync_pending.wait()          # block until a write signals us
        _sync_pending.clear()
        import time as _time
        _time.sleep(0.3)              # coalesce writes within 300 ms
        if not _SYNC_REMOTE:
            continue
        with _sync_lock:
            try:
                result = _subprocess.run(
                    ["rsync", "-az", "--checksum", DB_PATH, _SYNC_REMOTE],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    print(f"[DBSync] rsync failed: {result.stderr.strip()}")
            except Exception as e:
                print(f"[DBSync] sync error: {e}")


def _start_sync_thread():
    global _sync_thread
    if _sync_thread is None or not _sync_thread.is_alive():
        _sync_thread = _threading.Thread(target=_sync_worker, daemon=True, name="db-sync")
        _sync_thread.start()


def trigger_sync():
    """Signal the sync worker that a write just happened. Non-blocking."""
    if _SYNC_REMOTE:
        _sync_pending.set()


@contextmanager
def _conn():
    """Open DB connection. Triggers a sync push after every successful write."""
    con = sqlite3.connect(DB_PATH)
    if _ENCRYPTED:
        con.execute(f"PRAGMA key='{DB_KEY}'")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    class _TrackingConn:
        """Thin wrapper that tracks whether any write SQL was executed."""
        def __init__(self, conn):
            self._c = conn
            self.did_write = False
        def execute(self, sql, *args, **kwargs):
            if sql.strip()[:6].upper() in ("INSERT", "UPDATE", "DELETE", "REPLAC"):
                self.did_write = True
            return self._c.execute(sql, *args, **kwargs)
        def executescript(self, sql):
            self.did_write = True
            return self._c.executescript(sql)
        def commit(self):   return self._c.commit()
        def rollback(self): return self._c.rollback()
        def close(self):    return self._c.close()

    wrapper = _TrackingConn(con)
    try:
        yield wrapper
        con.commit()
        if wrapper.did_write:
            trigger_sync()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()




def init_db():
    """Create all tables if they don't exist. Safe to call on every startup."""
    _start_sync_thread()
    with _conn() as con:
        con.executescript("""
        -- ── Economy ──────────────────────────────────────────────────────────
        -- All Discord snowflake IDs stored as TEXT (too large for SQLite INTEGER)
        CREATE TABLE IF NOT EXISTS users (
            user_id     TEXT PRIMARY KEY,
            fc          INTEGER NOT NULL DEFAULT 0,
            cr          INTEGER NOT NULL DEFAULT 0,
            last_daily  TEXT,
            name_card   TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS inventory (
            user_id     TEXT NOT NULL,
            item_id     TEXT NOT NULL,
            acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, item_id)
        );

        -- ── Achievements ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS achievement_defs (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT,
            condition   TEXT,
            color       TEXT DEFAULT '#gold'
        );

        CREATE TABLE IF NOT EXISTS achievement_earned (
            user_id         TEXT NOT NULL,
            achievement_id  TEXT NOT NULL,
            earned_at       TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, achievement_id)
        );

        CREATE TABLE IF NOT EXISTS player_stats (
            user_id      TEXT PRIMARY KEY,
            games_played INTEGER NOT NULL DEFAULT 0,
            win_streak   INTEGER NOT NULL DEFAULT 0,
            loss_streak  INTEGER NOT NULL DEFAULT 0
        );

        -- ── AI Bans ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS ai_bans (
            user_id         TEXT PRIMARY KEY,
            expiry          REAL,
            scanner_flagged INTEGER NOT NULL DEFAULT 0,
            reason          TEXT,
            banned_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- ── Stmute ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS stmutes (
            user_id  TEXT PRIMARY KEY,
            until    TEXT NOT NULL,
            reason   TEXT
        );

        -- ── Callsigns ─────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS callsigns (
            user_id  TEXT PRIMARY KEY,
            callsign TEXT NOT NULL
        );

        -- ── Guild settings ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id TEXT NOT NULL,
            key      TEXT NOT NULL,
            value    TEXT,
            PRIMARY KEY (guild_id, key)
        );

        -- ── Reaction roles ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS reaction_roles (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   TEXT NOT NULL,
            message_id TEXT NOT NULL,
            emoji      TEXT NOT NULL,
            role_id    TEXT NOT NULL,
            UNIQUE (message_id, emoji)
        );

        -- ── Count channel ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS count_data (
            guild_id      TEXT PRIMARY KEY,
            channel_id    TEXT,
            current_count INTEGER NOT NULL DEFAULT 0,
            last_user_id  TEXT,
            safes         INTEGER NOT NULL DEFAULT 3
        );

        -- ── Tickets ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS ticket_meta (
            guild_id     TEXT PRIMARY KEY,
            ticket_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS ticket_logs (
            ticket_id  TEXT NOT NULL,
            guild_id   TEXT NOT NULL,
            transcript TEXT NOT NULL,
            closed_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (ticket_id, guild_id)
        );

        -- ── Captcha verified users ────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS verified_users (
            user_id     TEXT PRIMARY KEY,
            verified_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- ── Promo message counts ──────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS promo_msg_counts (
            user_id TEXT PRIMARY KEY,
            week    TEXT NOT NULL,
            count   INTEGER NOT NULL DEFAULT 0
        );

        -- ── Spotify tokens ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS spotify_tokens (
            user_id       TEXT PRIMARY KEY,
            access_token  TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at    REAL NOT NULL,
            linked_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- ── Dashboard sessions ────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS dashboard_sessions (
            session_id    TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL,
            discord_token TEXT NOT NULL,
            expires_at    REAL NOT NULL
        );

        -- ── Scanner logs ──────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS scan_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT NOT NULL,
            flagged_message TEXT,
            explanation     TEXT,
            raw_pass1       TEXT,
            scanned_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- ── Word filters ──────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS word_filters (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  TEXT NOT NULL,
            list_name TEXT NOT NULL,
            word      TEXT NOT NULL,
            action    TEXT NOT NULL DEFAULT 'delete',
            UNIQUE (guild_id, list_name, word)
        );

        -- ── Ticket types (named ticket categories) ───────────────────────────
        CREATE TABLE IF NOT EXISTS ticket_types (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    TEXT NOT NULL,
            name        TEXT NOT NULL,
            channel_id  TEXT,
            category_id TEXT,
            UNIQUE (guild_id, name)
        );

        -- ── Ticket type counters ──────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS ticket_type_counts (
            guild_id    TEXT NOT NULL,
            type_name   TEXT NOT NULL,
            count       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, type_name)
        );

        -- ── Ticket ephemeral questions ────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS ticket_questions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    TEXT NOT NULL,
            type_name   TEXT NOT NULL,
            question    TEXT NOT NULL,
            position    INTEGER NOT NULL DEFAULT 0
        );

        -- ── Appeal questions ──────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS appeal_questions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            question    TEXT NOT NULL,
            position    INTEGER NOT NULL DEFAULT 0
        );

        -- ── Appeal tickets ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS appeal_tickets (
            ticket_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            channel_id  TEXT,
            status      TEXT NOT NULL DEFAULT 'open',
            opened_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- ── Captcha ban pending (eligible for DM verify) ─────────────────────
        CREATE TABLE IF NOT EXISTS captcha_ban_pending (
            user_id     TEXT PRIMARY KEY,
            banned_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS devtools_access (
            user_id    TEXT PRIMARY KEY,
            granted_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- ── Ghost demoted roles ───────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS ghost_demoted_roles (
            user_id  TEXT PRIMARY KEY,
            role_ids TEXT NOT NULL DEFAULT '[]'
        );

        -- ── Log message cache (for log protection) ────────────────────────────
        CREATE TABLE IF NOT EXISTS log_message_cache (
            message_id  TEXT PRIMARY KEY,
            guild_id    TEXT NOT NULL,
            channel_id  TEXT,
            embed_json  TEXT NOT NULL,
            is_incident INTEGER NOT NULL DEFAULT 0
        );

        -- ── Media hash blacklist ──────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS media_hash_blacklist (
            hash TEXT PRIMARY KEY
        );

        -- ── Quotes ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS quotes (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL
        );
        """)


# ══════════════════════════════════════════════════════════════════════════════
# ECONOMY
# ══════════════════════════════════════════════════════════════════════════════

def get_user(user_id) -> dict:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if row:
            d = dict(row); d["user_id"] = int(d["user_id"]); return d
        con.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        return {"user_id": int(uid), "fc": 0, "cr": 0, "last_daily": None, "name_card": None}

def set_fc(user_id, amount: int):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT INTO users (user_id, fc) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET fc=?",
                    (uid, amount, amount))

def set_cr(user_id, amount: int):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT INTO users (user_id, cr) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET cr=?",
                    (uid, amount, amount))

def add_fc(user_id, amount: int) -> int:
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT INTO users (user_id, fc) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET fc=fc+?",
                    (uid, max(0, amount), amount))
        return con.execute("SELECT fc FROM users WHERE user_id=?", (uid,)).fetchone()[0]

def add_cr(user_id, amount: int) -> int:
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT INTO users (user_id, cr) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET cr=cr+?",
                    (uid, max(0, amount), amount))
        return con.execute("SELECT cr FROM users WHERE user_id=?", (uid,)).fetchone()[0]

def deduct_fc(user_id, amount: int) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT fc FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row or row[0] < amount: return False
        con.execute("UPDATE users SET fc=fc-? WHERE user_id=?", (amount, uid))
        return True

def deduct_cr(user_id, amount: int) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT cr FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row or row[0] < amount: return False
        con.execute("UPDATE users SET cr=cr-? WHERE user_id=?", (amount, uid))
        return True

def set_last_daily(user_id, date_str: str):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT INTO users (user_id, last_daily) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_daily=?",
                    (uid, date_str, date_str))

def set_name_card(user_id, text: str):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT INTO users (user_id, name_card) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET name_card=?",
                    (uid, text, text))

def get_fc_leaderboard(limit: int = 10) -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT user_id, fc FROM users ORDER BY fc DESC LIMIT ?", (limit,)).fetchall()
        return [{"user_id": int(r[0]), "fc": r[1]} for r in rows]

# ── Inventory ─────────────────────────────────────────────────────────────────

def get_inventory(user_id) -> list[str]:
    uid = _id(user_id)
    with _conn() as con:
        rows = con.execute("SELECT item_id FROM inventory WHERE user_id=?", (uid,)).fetchall()
        return [r[0] for r in rows]

def has_item(user_id, item_id: str) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        return bool(con.execute("SELECT 1 FROM inventory WHERE user_id=? AND item_id=?",
                                (uid, item_id)).fetchone())

def add_item(user_id, item_id: str):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO inventory (user_id, item_id) VALUES (?,?)", (uid, item_id))

def remove_item(user_id, item_id: str):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (uid, item_id))


# ══════════════════════════════════════════════════════════════════════════════
# ACHIEVEMENTS
# ══════════════════════════════════════════════════════════════════════════════

def get_all_achievement_defs() -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM achievement_defs").fetchall()]

def upsert_achievement_def(id: str, name: str, description: str, condition: str, color: str = "#gold"):
    with _conn() as con:
        con.execute("""INSERT INTO achievement_defs (id, name, description, condition, color)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=?, description=?, condition=?, color=?""",
                    (id, name, description, condition, color, name, description, condition, color))

def delete_achievement_def(id: str):
    with _conn() as con:
        con.execute("DELETE FROM achievement_defs WHERE id=?", (id,))
        con.execute("DELETE FROM achievement_earned WHERE achievement_id=?", (id,))

def has_achievement(user_id, achievement_id: str) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        return bool(con.execute("SELECT 1 FROM achievement_earned WHERE user_id=? AND achievement_id=?",
                                (uid, achievement_id)).fetchone())

def award_achievement(user_id, achievement_id: str):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO achievement_earned (user_id, achievement_id) VALUES (?,?)",
                    (uid, achievement_id))

def get_earned_achievements(user_id) -> list[str]:
    uid = _id(user_id)
    with _conn() as con:
        return [r[0] for r in con.execute(
            "SELECT achievement_id FROM achievement_earned WHERE user_id=?", (uid,)).fetchall()]

def reset_achievements(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM achievement_earned WHERE user_id=?", (uid,))

def get_player_stats(user_id) -> dict:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT * FROM player_stats WHERE user_id=?", (uid,)).fetchone()
        if row: return dict(row)
        con.execute("INSERT INTO player_stats (user_id) VALUES (?)", (uid,))
        return {"user_id": uid, "games_played": 0, "win_streak": 0, "loss_streak": 0}

def update_player_stats(user_id, games_played: int = None, win_streak: int = None, loss_streak: int = None):
    uid   = _id(user_id)
    stats = get_player_stats(uid)
    gp = games_played if games_played is not None else stats["games_played"]
    ws = win_streak   if win_streak   is not None else stats["win_streak"]
    ls = loss_streak  if loss_streak  is not None else stats["loss_streak"]
    with _conn() as con:
        con.execute("""INSERT INTO player_stats (user_id, games_played, win_streak, loss_streak)
                       VALUES (?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET games_played=?, win_streak=?, loss_streak=?""",
                    (uid, gp, ws, ls, gp, ws, ls))


# ══════════════════════════════════════════════════════════════════════════════
# AI BANS
# ══════════════════════════════════════════════════════════════════════════════

def is_ai_banned(user_id) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT expiry FROM ai_bans WHERE user_id=?", (uid,)).fetchone()
        if not row: return False
        if row[0] is None: return True
        return time.time() < row[0]

def is_scanner_flagged(user_id) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT scanner_flagged FROM ai_bans WHERE user_id=?", (uid,)).fetchone()
        return bool(row and row[0])

def set_ai_ban(user_id, expiry: float | None, scanner_flagged: bool = False, reason: str = None):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("""INSERT INTO ai_bans (user_id, expiry, scanner_flagged, reason)
                       VALUES (?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET expiry=?, scanner_flagged=?, reason=?""",
                    (uid, expiry, int(scanner_flagged), reason,
                     expiry, int(scanner_flagged), reason))

def lift_ai_ban(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM ai_bans WHERE user_id=?", (uid,))

def get_all_ai_bans() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM ai_bans ORDER BY banned_at DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r); d["user_id"] = int(d["user_id"]); result.append(d)
        return result

def add_scan_log(user_id, flagged_message: str, explanation: str, raw_pass1: str):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT INTO scan_logs (user_id, flagged_message, explanation, raw_pass1) VALUES (?,?,?,?)",
                    (uid, flagged_message, explanation, raw_pass1))

def get_scan_logs(user_id=None) -> list[dict]:
    with _conn() as con:
        if user_id:
            rows = con.execute("SELECT * FROM scan_logs WHERE user_id=? ORDER BY scanned_at DESC",
                               (_id(user_id),)).fetchall()
        else:
            rows = con.execute("SELECT * FROM scan_logs ORDER BY scanned_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# STMUTES
# ══════════════════════════════════════════════════════════════════════════════

def add_stmute(user_id, until: datetime.datetime, reason: str = None):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO stmutes (user_id, until, reason) VALUES (?,?,?)",
                    (uid, until.isoformat(), reason))

def remove_stmute(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM stmutes WHERE user_id=?", (uid,))

def get_all_stmutes() -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM stmutes").fetchall()]


# ══════════════════════════════════════════════════════════════════════════════
# CALLSIGNS
# ══════════════════════════════════════════════════════════════════════════════

def get_callsign(user_id) -> str | None:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT callsign FROM callsigns WHERE user_id=?", (uid,)).fetchone()
        return row[0] if row else None

def set_callsign(user_id, callsign: str):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO callsigns (user_id, callsign) VALUES (?,?)", (uid, callsign))

def delete_callsign(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM callsigns WHERE user_id=?", (uid,))

def get_all_callsigns() -> dict:
    with _conn() as con:
        rows = con.execute("SELECT user_id, callsign FROM callsigns").fetchall()
        return {r[0]: r[1] for r in rows}


# ══════════════════════════════════════════════════════════════════════════════
# GUILD SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(guild_id, key: str, default=None):
    gid = _id(guild_id)
    with _conn() as con:
        row = con.execute("SELECT value FROM guild_settings WHERE guild_id=? AND key=?",
                          (gid, key)).fetchone()
        return row[0] if row else default

def set_setting(guild_id, key: str, value):
    gid = _id(guild_id)
    v   = str(value) if value is not None else None
    with _conn() as con:
        con.execute("""INSERT INTO guild_settings (guild_id, key, value) VALUES (?,?,?)
                       ON CONFLICT(guild_id, key) DO UPDATE SET value=?""", (gid, key, v, v))

def delete_setting(guild_id, key: str):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("DELETE FROM guild_settings WHERE guild_id=? AND key=?", (gid, key))

def get_all_settings(guild_id) -> dict:
    gid = _id(guild_id)
    with _conn() as con:
        rows = con.execute("SELECT key, value FROM guild_settings WHERE guild_id=?", (gid,)).fetchall()
        return {r[0]: r[1] for r in rows}


# ══════════════════════════════════════════════════════════════════════════════
# REACTION ROLES
# ══════════════════════════════════════════════════════════════════════════════

def get_reaction_roles(guild_id) -> list[dict]:
    gid = _id(guild_id)
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM reaction_roles WHERE guild_id=?", (gid,)).fetchall()]

def add_reaction_role(guild_id, message_id, emoji: str, role_id):
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO reaction_roles (guild_id, message_id, emoji, role_id) VALUES (?,?,?,?)",
                    (_id(guild_id), _id(message_id), emoji, _id(role_id)))

def remove_reaction_role(message_id, emoji: str):
    with _conn() as con:
        con.execute("DELETE FROM reaction_roles WHERE message_id=? AND emoji=?", (_id(message_id), emoji))


# ══════════════════════════════════════════════════════════════════════════════
# COUNT CHANNEL
# ══════════════════════════════════════════════════════════════════════════════

def get_count_data(guild_id) -> dict:
    gid = _id(guild_id)
    with _conn() as con:
        row = con.execute("SELECT * FROM count_data WHERE guild_id=?", (gid,)).fetchone()
        if row: return dict(row)
        con.execute("INSERT INTO count_data (guild_id) VALUES (?)", (gid,))
        return {"guild_id": gid, "channel_id": None, "current_count": 0, "last_user_id": None, "safes": 3}

def set_count_channel(guild_id, channel_id):
    gid = _id(guild_id)
    cid = _id(channel_id)
    with _conn() as con:
        con.execute("""INSERT INTO count_data (guild_id, channel_id) VALUES (?,?)
                       ON CONFLICT(guild_id) DO UPDATE SET channel_id=?""",
                    (gid, cid, cid))

def update_count(guild_id, current_count: int, last_user_id):
    gid = _id(guild_id)
    lid = _id(last_user_id) if last_user_id is not None else None
    with _conn() as con:
        con.execute("""INSERT INTO count_data (guild_id, current_count, last_user_id) VALUES (?,?,?)
                       ON CONFLICT(guild_id) DO UPDATE SET current_count=?, last_user_id=?""",
                    (gid, current_count, lid, current_count, lid))

def use_safe(guild_id) -> int:
    gid = _id(guild_id)
    with _conn() as con:
        row = con.execute("SELECT safes FROM count_data WHERE guild_id=?", (gid,)).fetchone()
        if not row or row[0] <= 0: return -1
        new = row[0] - 1
        con.execute("UPDATE count_data SET safes=? WHERE guild_id=?", (new, gid))
        return new

def reset_count(guild_id):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("UPDATE count_data SET current_count=0, last_user_id=NULL WHERE guild_id=?", (gid,))

def set_safes(guild_id, safes: int):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("INSERT INTO count_data (guild_id, safes) VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET safes=?",
                    (gid, safes, safes))


# ══════════════════════════════════════════════════════════════════════════════
# VERIFIED USERS
# ══════════════════════════════════════════════════════════════════════════════

def is_verified(user_id) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        return bool(con.execute("SELECT 1 FROM verified_users WHERE user_id=?", (uid,)).fetchone())

def set_verified(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO verified_users (user_id) VALUES (?)", (uid,))

def unverify(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM verified_users WHERE user_id=?", (uid,))

def get_all_verified() -> list[int]:
    with _conn() as con:
        return [int(r[0]) for r in con.execute("SELECT user_id FROM verified_users").fetchall()]


# ══════════════════════════════════════════════════════════════════════════════
# PROMO MESSAGE COUNTS
# ══════════════════════════════════════════════════════════════════════════════

def get_promo_count(user_id, week: str) -> int:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT count FROM promo_msg_counts WHERE user_id=? AND week=?",
                          (uid, week)).fetchone()
        return row[0] if row else 0

def increment_promo_count(user_id, week: str):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("""INSERT INTO promo_msg_counts (user_id, week, count) VALUES (?,?,1)
                       ON CONFLICT(user_id) DO UPDATE SET
                       count = CASE WHEN week=? THEN count+1 ELSE 1 END,
                       week  = ?""", (uid, week, week, week))

def get_all_promo_counts(week: str) -> dict:
    with _conn() as con:
        rows = con.execute("SELECT user_id, count FROM promo_msg_counts WHERE week=?", (week,)).fetchall()
        return {r[0]: r[1] for r in rows}


# ══════════════════════════════════════════════════════════════════════════════
# TICKETS
# ══════════════════════════════════════════════════════════════════════════════

def ticket_next_number(guild_id) -> int:
    """Atomically increment and return the next ticket number."""
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("""INSERT INTO ticket_meta (guild_id, ticket_count) VALUES (?,1)
                       ON CONFLICT(guild_id) DO UPDATE SET ticket_count=ticket_count+1""", (gid,))
        return con.execute("SELECT ticket_count FROM ticket_meta WHERE guild_id=?", (gid,)).fetchone()[0]

def ticket_save_log(guild_id, ticket_id: str, transcript: str):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("""INSERT INTO ticket_logs (ticket_id, guild_id, transcript) VALUES (?,?,?)
                       ON CONFLICT(ticket_id, guild_id) DO UPDATE SET transcript=?, closed_at=datetime('now')""",
                    (ticket_id, gid, transcript, transcript))

def ticket_get_log(guild_id, ticket_id: str) -> str | None:
    gid = _id(guild_id)
    with _conn() as con:
        row = con.execute("SELECT transcript FROM ticket_logs WHERE guild_id=? AND ticket_id=?",
                          (gid, ticket_id)).fetchone()
        return row[0] if row else None

def ticket_latest_id(guild_id) -> str | None:
    gid = _id(guild_id)
    with _conn() as con:
        row = con.execute("SELECT ticket_id FROM ticket_logs WHERE guild_id=? ORDER BY closed_at DESC LIMIT 1",
                          (gid,)).fetchone()
        return row[0] if row else None

def ticket_get_count(guild_id) -> int:
    gid = _id(guild_id)
    with _conn() as con:
        row = con.execute("SELECT ticket_count FROM ticket_meta WHERE guild_id=?", (gid,)).fetchone()
        return row[0] if row else 0


# ══════════════════════════════════════════════════════════════════════════════
# SPOTIFY TOKENS
# ══════════════════════════════════════════════════════════════════════════════

def get_spotify_token(user_id) -> dict | None:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT * FROM spotify_tokens WHERE user_id=?", (uid,)).fetchone()
        return dict(row) if row else None

def set_spotify_token(user_id, access_token: str, refresh_token: str, expires_at: float):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("""INSERT INTO spotify_tokens (user_id, access_token, refresh_token, expires_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET access_token=?, refresh_token=?, expires_at=?""",
                    (uid, access_token, refresh_token, expires_at,
                     access_token, refresh_token, expires_at))

def delete_spotify_token(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM spotify_tokens WHERE user_id=?", (uid,))


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD SESSIONS
# ══════════════════════════════════════════════════════════════════════════════

def create_session(session_id: str, user_id, discord_token: str, expires_at: float):
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO dashboard_sessions (session_id, user_id, discord_token, expires_at) VALUES (?,?,?,?)",
                    (session_id, _id(user_id), discord_token, expires_at))

def get_session(session_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM dashboard_sessions WHERE session_id=? AND expires_at>?",
                          (session_id, time.time())).fetchone()
        return dict(row) if row else None

def delete_session(session_id: str):
    with _conn() as con:
        con.execute("DELETE FROM dashboard_sessions WHERE session_id=?", (session_id,))

def purge_expired_sessions():
    with _conn() as con:
        con.execute("DELETE FROM dashboard_sessions WHERE expires_at<?", (time.time(),))


# ══════════════════════════════════════════════════════════════════════════════
# WORD FILTERS
# ══════════════════════════════════════════════════════════════════════════════

def get_word_filters(guild_id) -> dict:
    gid = _id(guild_id)
    with _conn() as con:
        rows = con.execute("SELECT list_name, word FROM word_filters WHERE guild_id=?", (gid,)).fetchall()
        result = {}
        for r in rows:
            result.setdefault(r[0], []).append(r[1])
        return result

def add_word_filter(guild_id, list_name: str, word: str, action: str = "delete"):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO word_filters (guild_id, list_name, word, action) VALUES (?,?,?,?)",
                    (gid, list_name, word, action))

def remove_word_filter(guild_id, list_name: str, word: str):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("DELETE FROM word_filters WHERE guild_id=? AND list_name=? AND word=?",
                    (gid, list_name, word))




# ══════════════════════════════════════════════════════════════════════════════
# GUILD JSON CONFIGS
# Stores arbitrary JSON blobs in guild_settings. Used for:
# ai_channel, staff_heartbeat, account_age_wall, dsm_config,
# ghost_mode, promo_last_run, automod, logs_config
# ══════════════════════════════════════════════════════════════════════════════

import json as _json

def get_json_config(guild_id, key: str, default=None):
    raw = get_setting(guild_id, key)
    if raw is None:
        return default
    try:
        return _json.loads(raw)
    except Exception:
        return default

def set_json_config(guild_id, key: str, data):
    set_setting(guild_id, key, _json.dumps(data))

def delete_json_config(guild_id, key: str):
    delete_setting(guild_id, key)


# ── Global JSON configs (not guild-scoped) ────────────────────────────────────
# Stored in guild_settings with guild_id='global'

GLOBAL = 'global'

def get_global_config(key: str, default=None):
    return get_json_config(GLOBAL, key, default)

def set_global_config(key: str, data):
    set_json_config(GLOBAL, key, data)


# ══════════════════════════════════════════════════════════════════════════════
# MEDIA HASH BLACKLIST
# ══════════════════════════════════════════════════════════════════════════════

def get_media_hashes() -> list:
    with _conn() as con:
        return [r[0] for r in con.execute("SELECT hash FROM media_hash_blacklist").fetchall()]

def add_media_hash(h: str):
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO media_hash_blacklist (hash) VALUES (?)", (h,))

def remove_media_hash(h: str):
    with _conn() as con:
        con.execute("DELETE FROM media_hash_blacklist WHERE hash=?", (h,))


# ══════════════════════════════════════════════════════════════════════════════
# GHOST DEMOTED ROLES
# ══════════════════════════════════════════════════════════════════════════════

def get_demoted_roles(user_id) -> list:
    uid = _id(user_id)
    with _conn() as con:
        row = con.execute("SELECT role_ids FROM ghost_demoted_roles WHERE user_id=?", (uid,)).fetchone()
        return _json.loads(row[0]) if row else []

def set_demoted_roles(user_id, role_ids: list):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO ghost_demoted_roles (user_id, role_ids) VALUES (?,?)",
                    (uid, _json.dumps(role_ids)))

def pop_demoted_roles(user_id) -> list:
    uid = _id(user_id)
    roles = get_demoted_roles(uid)
    with _conn() as con:
        con.execute("DELETE FROM ghost_demoted_roles WHERE user_id=?", (uid,))
    return roles


# ══════════════════════════════════════════════════════════════════════════════
# LOG MESSAGE CACHE
# ══════════════════════════════════════════════════════════════════════════════

def log_cache_add(message_id: int, guild_id, channel_id, embed_dict: dict, is_incident: bool = False):
    with _conn() as con:
        con.execute("""INSERT OR REPLACE INTO log_message_cache
                       (message_id, guild_id, channel_id, embed_json, is_incident)
                       VALUES (?,?,?,?,?)""",
                    (str(message_id), _id(guild_id), _id(channel_id) if channel_id else None,
                     _json.dumps(embed_dict), int(is_incident)))

def log_cache_pop(message_id: int) -> dict | None:
    mid = str(message_id)
    with _conn() as con:
        row = con.execute("SELECT * FROM log_message_cache WHERE message_id=?", (mid,)).fetchone()
        if not row:
            return None
        con.execute("DELETE FROM log_message_cache WHERE message_id=?", (mid,))
        d = dict(row)
        d['embed'] = _json.loads(d.pop('embed_json'))
        return d


# ══════════════════════════════════════════════════════════════════════════════
# QUOTES
# ══════════════════════════════════════════════════════════════════════════════

def get_quotes() -> list:
    with _conn() as con:
        return [r[0] for r in con.execute("SELECT content FROM quotes ORDER BY id").fetchall()]

def add_quote(content: str):
    with _conn() as con:
        con.execute("INSERT INTO quotes (content) VALUES (?)", (content,))

def remove_quote(index: int):
    """Remove quote by 1-based index."""
    with _conn() as con:
        rows = con.execute("SELECT id FROM quotes ORDER BY id").fetchall()
        if 0 < index <= len(rows):
            con.execute("DELETE FROM quotes WHERE id=?", (rows[index - 1][0],))

def save_quotes(quotes: list):
    """Overwrite all quotes at once."""
    with _conn() as con:
        con.execute("DELETE FROM quotes")
        for q in quotes:
            con.execute("INSERT INTO quotes (content) VALUES (?)", (q,))


# ══════════════════════════════════════════════════════════════════════════════
# DEVTOOLS ACCESS
# ══════════════════════════════════════════════════════════════════════════════

def has_devtools_access(user_id) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        return bool(con.execute("SELECT 1 FROM devtools_access WHERE user_id=?", (uid,)).fetchone())

def grant_devtools_access(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO devtools_access (user_id) VALUES (?)", (uid,))

def revoke_devtools_access(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM devtools_access WHERE user_id=?", (uid,))

def get_all_devtools_access() -> list:
    with _conn() as con:
        return [int(r[0]) for r in con.execute("SELECT user_id FROM devtools_access ORDER BY granted_at").fetchall()]


# ══════════════════════════════════════════════════════════════════════════════
# TICKET TYPES
# ══════════════════════════════════════════════════════════════════════════════

def create_ticket_type(guild_id, name: str, channel_id=None, category_id=None):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("""INSERT INTO ticket_types (guild_id, name, channel_id, category_id)
                       VALUES (?,?,?,?)
                       ON CONFLICT(guild_id, name) DO UPDATE SET channel_id=?, category_id=?""",
                    (gid, name, _id(channel_id) if channel_id else None,
                     _id(category_id) if category_id else None,
                     _id(channel_id) if channel_id else None,
                     _id(category_id) if category_id else None))

def get_ticket_types(guild_id) -> list:
    gid = _id(guild_id)
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM ticket_types WHERE guild_id=? ORDER BY name", (gid,)).fetchall()]

def get_ticket_type(guild_id, name: str) -> dict | None:
    gid = _id(guild_id)
    with _conn() as con:
        row = con.execute("SELECT * FROM ticket_types WHERE guild_id=? AND name=?", (gid, name)).fetchone()
        return dict(row) if row else None

def set_ticket_type_category(guild_id, name: str, category_id):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("UPDATE ticket_types SET category_id=? WHERE guild_id=? AND name=?",
                    (_id(category_id) if category_id else None, gid, name))

def ticket_type_next_number(guild_id, type_name: str) -> int:
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("""INSERT INTO ticket_type_counts (guild_id, type_name, count) VALUES (?,?,1)
                       ON CONFLICT(guild_id, type_name) DO UPDATE SET count=count+1""", (gid, type_name))
        return con.execute("SELECT count FROM ticket_type_counts WHERE guild_id=? AND type_name=?",
                           (gid, type_name)).fetchone()[0]


# ══════════════════════════════════════════════════════════════════════════════
# TICKET EPHEMERAL QUESTIONS
# ══════════════════════════════════════════════════════════════════════════════

def get_ticket_questions(guild_id, type_name: str) -> list:
    gid = _id(guild_id)
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM ticket_questions WHERE guild_id=? AND type_name=? ORDER BY position",
            (gid, type_name)).fetchall()]

def add_ticket_question(guild_id, type_name: str, question: str, position: int = None):
    gid = _id(guild_id)
    with _conn() as con:
        if position is None:
            row = con.execute("SELECT MAX(position) FROM ticket_questions WHERE guild_id=? AND type_name=?",
                              (gid, type_name)).fetchone()
            position = (row[0] or 0) + 1
        con.execute("INSERT INTO ticket_questions (guild_id, type_name, question, position) VALUES (?,?,?,?)",
                    (gid, type_name, question, position))

def remove_ticket_question(question_id: int):
    with _conn() as con:
        con.execute("DELETE FROM ticket_questions WHERE id=?", (question_id,))

def clear_ticket_questions(guild_id, type_name: str):
    gid = _id(guild_id)
    with _conn() as con:
        con.execute("DELETE FROM ticket_questions WHERE guild_id=? AND type_name=?", (gid, type_name))


# ══════════════════════════════════════════════════════════════════════════════
# APPEAL QUESTIONS
# ══════════════════════════════════════════════════════════════════════════════

def get_appeal_questions() -> list:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM appeal_questions ORDER BY position").fetchall()]

def add_appeal_question(question: str, position: int = None):
    with _conn() as con:
        if position is None:
            row = con.execute("SELECT MAX(position) FROM appeal_questions").fetchone()
            position = (row[0] or 0) + 1
        con.execute("INSERT INTO appeal_questions (question, position) VALUES (?,?)", (question, position))

def remove_appeal_question(question_id: int):
    with _conn() as con:
        con.execute("DELETE FROM appeal_questions WHERE id=?", (question_id,))

def clear_appeal_questions():
    with _conn() as con:
        con.execute("DELETE FROM appeal_questions")


# ══════════════════════════════════════════════════════════════════════════════
# APPEAL TICKETS
# ══════════════════════════════════════════════════════════════════════════════

def create_appeal_ticket(user_id, channel_id) -> int:
    uid = _id(user_id)
    cid = _id(channel_id)
    with _conn() as con:
        con.execute("INSERT INTO appeal_tickets (user_id, channel_id) VALUES (?,?)", (uid, cid))
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]

def get_appeal_ticket_by_channel(channel_id) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM appeal_tickets WHERE channel_id=?", (_id(channel_id),)).fetchone()
        return dict(row) if row else None

def close_appeal_ticket(channel_id):
    with _conn() as con:
        con.execute("UPDATE appeal_tickets SET status='closed' WHERE channel_id=?", (_id(channel_id),))

def has_open_appeal(user_id) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        return bool(con.execute(
            "SELECT 1 FROM appeal_tickets WHERE user_id=? AND status='open'", (uid,)).fetchone())


# ══════════════════════════════════════════════════════════════════════════════
# CAPTCHA BAN PENDING
# ══════════════════════════════════════════════════════════════════════════════

def set_captcha_ban_pending(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO captcha_ban_pending (user_id) VALUES (?)", (uid,))

def is_captcha_ban_pending(user_id) -> bool:
    uid = _id(user_id)
    with _conn() as con:
        return bool(con.execute("SELECT 1 FROM captcha_ban_pending WHERE user_id=?", (uid,)).fetchone())

def clear_captcha_ban_pending(user_id):
    uid = _id(user_id)
    with _conn() as con:
        con.execute("DELETE FROM captcha_ban_pending WHERE user_id=?", (uid,))


# ══════════════════════════════════════════════════════════════════════════════
# TICKET VIEWER ROLES
# ══════════════════════════════════════════════════════════════════════════════

def get_ticket_viewer_roles(guild_id, type_name: str) -> list:
    """Returns list of role IDs (as ints) that can see tickets of this type."""
    gid = _id(guild_id)
    data = get_json_config(gid, f'ticket_viewer_roles_{type_name}', [])
    return [int(r) for r in data]

def add_ticket_viewer_role(guild_id, type_name: str, role_id):
    gid = _id(guild_id)
    key = f'ticket_viewer_roles_{type_name}'
    roles = get_json_config(gid, key, [])
    rid = int(_id(role_id))
    if rid not in roles:
        roles.append(rid)
        set_json_config(gid, key, roles)

def remove_ticket_viewer_role(guild_id, type_name: str, role_id):
    gid = _id(guild_id)
    key = f'ticket_viewer_roles_{type_name}'
    roles = get_json_config(gid, key, [])
    rid = int(_id(role_id))
    if rid in roles:
        roles.remove(rid)
        set_json_config(gid, key, roles)
