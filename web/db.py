import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

# 路由 key 不能与 homepage 自身的路径冲突。
RESERVED_ROUTE_KEYS = {"api", "static", "login", "logout", "favicon.ico", "health"}

# 活跃记录最多保留的条数。
MAX_ACTIVE_LOG = 100_000

# 默认会话时长（天）。
DEFAULT_SESSION_DAYS = 30

# 首次启动时种入的默认路由：(key, 大名, 上游 host, 上游 port)。
DEFAULT_ROUTES = [
    ("ai-buddy", "AI Buddy", "127.0.0.1", 8080),
    ("xiangyun", "博弘翔云1号私募证券投资基金A", "127.0.0.1", 8848),
    ("bill", "账单", "127.0.0.1", 5057),
]

_DB_PATH = ""


def configure(db_path: str) -> None:
    global _DB_PATH
    _DB_PATH = db_path


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db(admin_initial_password: str) -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS routes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            key           TEXT UNIQUE NOT NULL,
            display_name  TEXT NOT NULL,
            upstream_host TEXT NOT NULL,
            upstream_port INTEGER NOT NULL,
            sort_order    INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_routes (
            user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, route_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS active_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            ip       TEXT NOT NULL,
            method   TEXT NOT NULL,
            path     TEXT NOT NULL,
            ts       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_active_username ON active_log(username);
        """
    )

    # 种入 admin（不存在时）。
    row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES(?,?,1,?)",
            ("admin", generate_password_hash(admin_initial_password), _now()),
        )

    # 种入默认路由（routes 表为空时）。
    cnt = conn.execute("SELECT COUNT(*) AS c FROM routes").fetchone()["c"]
    if cnt == 0:
        for i, (key, name, host, port) in enumerate(DEFAULT_ROUTES):
            conn.execute(
                "INSERT INTO routes(key, display_name, upstream_host, upstream_port, sort_order, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (key, name, host, port, i, _now()),
            )

    # 种入默认会话时长。
    if conn.execute("SELECT value FROM settings WHERE key='session_days'").fetchone() is None:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('session_days', ?)",
            (str(DEFAULT_SESSION_DAYS),),
        )

    conn.commit()
    conn.close()


# ---------- settings ----------

def get_session_days() -> int:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key='session_days'").fetchone()
    conn.close()
    return int(row["value"]) if row else DEFAULT_SESSION_DAYS


def set_session_days(days: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE settings SET value=? WHERE key='session_days'", (str(days),))
    conn.commit()
    conn.close()


# ---------- users ----------

def get_user(username: str) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row


def list_users() -> list[dict]:
    conn = get_conn()
    users = conn.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id").fetchall()
    result = []
    for u in users:
        keys = [
            r["key"]
            for r in conn.execute(
                "SELECT rt.key FROM user_routes ur JOIN routes rt ON rt.id=ur.route_id "
                "WHERE ur.user_id=? ORDER BY rt.sort_order",
                (u["id"],),
            ).fetchall()
        ]
        result.append(
            {
                "id": u["id"],
                "username": u["username"],
                "is_admin": bool(u["is_admin"]),
                "created_at": u["created_at"],
                "routes": keys,
            }
        )
    conn.close()
    return result


def create_user(username: str, password_hash: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES(?,?,0,?)",
        (username, password_hash, _now()),
    )
    conn.commit()
    conn.close()


def delete_user(user_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE id=? AND is_admin=0", (user_id,))
    conn.commit()
    conn.close()


def update_password(user_id: int, password_hash: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
    conn.commit()
    conn.close()


def set_user_routes(user_id: int, route_keys: list[str]) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM user_routes WHERE user_id=?", (user_id,))
    for key in route_keys:
        r = conn.execute("SELECT id FROM routes WHERE key=?", (key,)).fetchone()
        if r is not None:
            conn.execute(
                "INSERT OR IGNORE INTO user_routes(user_id, route_id) VALUES(?,?)",
                (user_id, r["id"]),
            )
    conn.commit()
    conn.close()


# ---------- routes ----------

def list_routes() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, key, display_name, upstream_host, upstream_port, sort_order "
        "FROM routes ORDER BY sort_order, id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_route_by_key(key: str) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM routes WHERE key=?", (key,)).fetchone()
    conn.close()
    return row


def visible_routes_for(user_id: int, is_admin: bool) -> list[dict]:
    conn = get_conn()
    if is_admin:
        rows = conn.execute(
            "SELECT key, display_name FROM routes ORDER BY sort_order, id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT rt.key, rt.display_name FROM user_routes ur "
            "JOIN routes rt ON rt.id=ur.route_id WHERE ur.user_id=? "
            "ORDER BY rt.sort_order, rt.id",
            (user_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def user_can_see_route(user_id: int, is_admin: bool, key: str) -> bool:
    if is_admin:
        return get_route_by_key(key) is not None
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM user_routes ur JOIN routes rt ON rt.id=ur.route_id "
        "WHERE ur.user_id=? AND rt.key=?",
        (user_id, key),
    ).fetchone()
    conn.close()
    return row is not None


def add_route(key: str, display_name: str, host: str, port: int) -> None:
    conn = get_conn()
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM routes").fetchone()["n"]
    conn.execute(
        "INSERT INTO routes(key, display_name, upstream_host, upstream_port, sort_order, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (key, display_name, host, port, nxt, _now()),
    )
    conn.commit()
    conn.close()


def update_route(route_id: int, display_name: str, host: str, port: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE routes SET display_name=?, upstream_host=?, upstream_port=? WHERE id=?",
        (display_name, host, port, route_id),
    )
    conn.commit()
    conn.close()


def delete_route(route_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM routes WHERE id=?", (route_id,))
    conn.commit()
    conn.close()


# ---------- active log ----------

def log_active(username: str, ip: str, method: str, path: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO active_log(username, ip, method, path, ts) VALUES(?,?,?,?,?)",
        (username, ip, method, path, _now()),
    )
    # 维持封顶：删掉超出 MAX_ACTIVE_LOG 的最旧记录。
    conn.execute(
        "DELETE FROM active_log WHERE id <= "
        "(SELECT MAX(id) FROM active_log) - ?",
        (MAX_ACTIVE_LOG,),
    )
    conn.commit()
    conn.close()


def last_seen_per_user() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT u.username, "
        "(SELECT MAX(a.ts) FROM active_log a WHERE a.username=u.username) AS last_ts "
        "FROM users u ORDER BY u.username"
    ).fetchall()
    conn.close()
    return [{"username": r["username"], "last_ts": r["last_ts"]} for r in rows]


def query_active_log(username: str | None, page: int, per_page: int) -> dict:
    conn = get_conn()
    where = ""
    params: list = []
    if username:
        where = "WHERE username=?"
        params.append(username)
    total = conn.execute(f"SELECT COUNT(*) AS c FROM active_log {where}", params).fetchone()["c"]
    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT username, ip, method, path, ts FROM active_log {where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()
    conn.close()
    return {"total": total, "rows": [dict(r) for r in rows]}
