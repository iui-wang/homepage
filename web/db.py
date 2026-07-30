import math
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

# 路由 key 不能与 homepage 自身的路径冲突。
RESERVED_ROUTE_KEYS = {"api", "static", "login", "logout", "favicon.ico", "health"}

# 活跃记录最多保留的条数。
MAX_ACTIVE_LOG = 100_000

# 机器监控采样间隔（秒）与降采样目标点数（单次查询返回的曲线点上限）。
METRICS_INTERVAL = 10
METRICS_MAX_POINTS = 400

# 默认会话时长（天）。
DEFAULT_SESSION_DAYS = 30

# 首次启动时种入的机器清单：(slug, 显示名, 排序)。URL 第一级路径段就是机器 slug。
DEFAULT_MACHINES = [
    ("tokyo", "东京腾讯云", 0),
    ("shanghai", "上海阿里云", 1),
    ("m720q", "联想 M720Q", 2),
    ("shin", "东京シンVPS", 3),
]

# 首次启动时种入的默认路由：(machine, key, 大名, 上游 host, 上游 port)。
DEFAULT_ROUTES = [
    ("tokyo", "xiangyun", "博弘翔云1号私募证券投资基金A", "127.0.0.1", 8848),
    ("tokyo", "bill", "账单", "127.0.0.1", 5057),
]

# 旧库迁移时按上游 host 回填 routes.machine 的映射（见 _migrate_routes_machine）。
HOST_TO_MACHINE = {
    "127.0.0.1": "tokyo",
    "10.77.0.1": "tokyo",
    "10.77.0.2": "shanghai",
    "10.77.0.3": "shin",
    "10.77.0.6": "m720q",
}

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


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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
        CREATE TABLE IF NOT EXISTS machines (
            slug         TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            sort_order   INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS routes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            machine       TEXT NOT NULL DEFAULT 'tokyo',
            key           TEXT NOT NULL,
            display_name  TEXT NOT NULL,
            upstream_host TEXT NOT NULL,
            upstream_port INTEGER NOT NULL,
            strip_prefix  INTEGER NOT NULL DEFAULT 0,
            sort_order    INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            UNIQUE(machine, key)
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
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            username  TEXT NOT NULL,
            ip        TEXT NOT NULL,
            method    TEXT NOT NULL,
            path      TEXT NOT NULL,
            start_ts  TEXT NOT NULL,
            end_ts    TEXT NOT NULL,
            count     INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_active_username ON active_log(username);
        CREATE TABLE IF NOT EXISTS metrics (
            ts          INTEGER PRIMARY KEY,
            cpu_pct     REAL    NOT NULL,
            mem_pct     REAL    NOT NULL,
            mem_used    INTEGER NOT NULL,
            mem_total   INTEGER NOT NULL,
            disk_pct    REAL    NOT NULL,
            disk_used   INTEGER NOT NULL,
            disk_total  INTEGER NOT NULL
        );
        """
    )

    # admin 密码以 config.toml 为唯一来源，每次启动都同步。
    new_hash = generate_password_hash(admin_initial_password)
    row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES(?,?,1,?)",
            ("admin", new_hash, _now()),
        )
    else:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE username='admin'",
            (new_hash,),
        )

    # 种入机器清单（INSERT OR IGNORE，已有库只补缺失的）。
    for slug, name, order in DEFAULT_MACHINES:
        conn.execute(
            "INSERT OR IGNORE INTO machines(slug, display_name, sort_order) VALUES(?,?,?)",
            (slug, name, order),
        )

    # 为已存在的旧数据库补列（新建库已在 CREATE TABLE 里包含）。
    try:
        conn.execute("ALTER TABLE routes ADD COLUMN strip_prefix INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 列已存在，忽略

    # 旧库 routes 表没有 machine 列时重建（唯一约束 key → (machine, key)），
    # 再按上游 host 回填 machine。幂等：列已存在则整个跳过。
    if "machine" not in _table_cols(conn, "routes"):
        conn.commit()  # PRAGMA foreign_keys 在事务内是 no-op，先结束未决事务
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(
            """
            CREATE TABLE routes_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                machine       TEXT NOT NULL DEFAULT 'tokyo',
                key           TEXT NOT NULL,
                display_name  TEXT NOT NULL,
                upstream_host TEXT NOT NULL,
                upstream_port INTEGER NOT NULL,
                strip_prefix  INTEGER NOT NULL DEFAULT 0,
                sort_order    INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                UNIQUE(machine, key)
            );
            INSERT INTO routes_new(id, key, display_name, upstream_host, upstream_port, strip_prefix, sort_order, created_at)
                SELECT id, key, display_name, upstream_host, upstream_port, strip_prefix, sort_order, created_at FROM routes;
            DROP TABLE routes;
            ALTER TABLE routes_new RENAME TO routes;
            """
        )
        conn.execute("PRAGMA foreign_keys=ON")
        for host, machine in HOST_TO_MACHINE.items():
            conn.execute("UPDATE routes SET machine=? WHERE upstream_host=?", (machine, host))

    # 种入默认路由（routes 表为空时）。
    cnt = conn.execute("SELECT COUNT(*) AS c FROM routes").fetchone()["c"]
    if cnt == 0:
        for i, (machine, key, name, host, port) in enumerate(DEFAULT_ROUTES):
            conn.execute(
                "INSERT INTO routes(machine, key, display_name, upstream_host, upstream_port, sort_order, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (machine, key, name, host, port, i, _now()),
            )

    # 种入默认会话时长。
    if conn.execute("SELECT value FROM settings WHERE key='session_days'").fetchone() is None:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('session_days', ?)",
            (str(DEFAULT_SESSION_DAYS),),
        )

    # 迁移 active_log：旧表用 ts 单列，新表改为 start_ts/end_ts/count，同时清空历史数据。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(active_log)").fetchall()}
    if "ts" in cols and "start_ts" not in cols:
        conn.executescript(
            """
            DROP TABLE IF EXISTS active_log;
            CREATE TABLE active_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                username  TEXT NOT NULL,
                ip        TEXT NOT NULL,
                method    TEXT NOT NULL,
                path      TEXT NOT NULL,
                start_ts  TEXT NOT NULL,
                end_ts    TEXT NOT NULL,
                count     INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_active_username ON active_log(username);
            """
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
        # 同一 key 可能有多台机器的路由行，授权按 key 去重展示。
        keys = list(
            dict.fromkeys(
                r["key"]
                for r in conn.execute(
                    "SELECT rt.key FROM user_routes ur JOIN routes rt ON rt.id=ur.route_id "
                    "WHERE ur.user_id=? ORDER BY rt.sort_order",
                    (u["id"],),
                ).fetchall()
            )
        )
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
    """按服务 key 授权：同一 key 在所有机器上的路由行都会授予该用户。"""
    conn = get_conn()
    conn.execute("DELETE FROM user_routes WHERE user_id=?", (user_id,))
    for key in route_keys:
        for r in conn.execute("SELECT id FROM routes WHERE key=?", (key,)).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO user_routes(user_id, route_id) VALUES(?,?)",
                (user_id, r["id"]),
            )
    conn.commit()
    conn.close()


# ---------- machines ----------

def list_machines() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, display_name, sort_order FROM machines ORDER BY sort_order, slug"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def machine_exists(slug: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM machines WHERE slug=?", (slug,)).fetchone()
    conn.close()
    return row is not None


# ---------- routes ----------

def list_routes() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, machine, key, display_name, upstream_host, upstream_port, strip_prefix, sort_order "
        "FROM routes ORDER BY sort_order, id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_route(machine: str, key: str) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM routes WHERE machine=? AND key=?", (machine, key)).fetchone()
    conn.close()
    return row


def visible_routes_for(user_id: int, is_admin: bool) -> list[dict]:
    """按服务 key 聚合可见路由：每项 {key, display_name, machines:[{slug, display_name}]}，
    machines 按机器表 sort_order 排序。"""
    conn = get_conn()
    base_sql = (
        "SELECT rt.key, rt.display_name, m.slug, m.display_name AS machine_name, m.sort_order AS machine_sort "
        "FROM {src} JOIN machines m ON m.slug=rt.machine "
        "{where} ORDER BY rt.sort_order, rt.id"
    )
    if is_admin:
        rows = conn.execute(
            base_sql.format(src="routes rt", where="")
        ).fetchall()
    else:
        rows = conn.execute(
            base_sql.format(
                src="user_routes ur JOIN routes rt ON rt.id=ur.route_id",
                where="WHERE ur.user_id=?",
            ),
            (user_id,),
        ).fetchall()
    conn.close()
    result: list[dict] = []
    by_key: dict[str, dict] = {}
    for r in rows:
        entry = by_key.get(r["key"])
        if entry is None:
            entry = {"key": r["key"], "display_name": r["display_name"], "machines": []}
            by_key[r["key"]] = entry
            result.append(entry)
        entry["machines"].append(
            {"slug": r["slug"], "display_name": r["machine_name"], "sort_order": r["machine_sort"]}
        )
    for entry in result:
        entry["machines"].sort(key=lambda m: m["sort_order"])
        for m in entry["machines"]:
            del m["sort_order"]
    return result


def user_can_see_route(user_id: int, is_admin: bool, key: str) -> bool:
    if is_admin:
        conn = get_conn()
        row = conn.execute("SELECT 1 FROM routes WHERE key=?", (key,)).fetchone()
        conn.close()
        return row is not None
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM user_routes ur JOIN routes rt ON rt.id=ur.route_id "
        "WHERE ur.user_id=? AND rt.key=?",
        (user_id, key),
    ).fetchone()
    conn.close()
    return row is not None


def add_route(machine: str, key: str, display_name: str, host: str, port: int, strip_prefix: bool = False) -> None:
    conn = get_conn()
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM routes").fetchone()["n"]
    conn.execute(
        "INSERT INTO routes(machine, key, display_name, upstream_host, upstream_port, strip_prefix, sort_order, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (machine, key, display_name, host, port, int(strip_prefix), nxt, _now()),
    )
    conn.commit()
    conn.close()


def update_route(route_id: int, machine: str, display_name: str, host: str, port: int, strip_prefix: bool = False) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE routes SET machine=?, display_name=?, upstream_host=?, upstream_port=?, strip_prefix=? WHERE id=?",
        (machine, display_name, host, port, int(strip_prefix), route_id),
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
    now = _now()
    # 合并键含 ip+method，避免不同来源/方法的请求被错并到一条；
    # 窗口以 start_ts 为基准（而非 end_ts），单条记录最长只覆盖 60 秒，防止持续活跃时被无限拉长。
    row = conn.execute(
        "SELECT id, start_ts FROM active_log "
        "WHERE username=? AND path=? AND ip=? AND method=? ORDER BY id DESC LIMIT 1",
        (username, path, ip, method),
    ).fetchone()
    if row:
        delta = datetime.strptime(now, "%Y-%m-%d %H:%M:%S") - datetime.strptime(row["start_ts"], "%Y-%m-%d %H:%M:%S")
        if 0 <= delta.total_seconds() <= 60:
            conn.execute("UPDATE active_log SET end_ts=?, count=count+1 WHERE id=?", (now, row["id"]))
            conn.commit()
            conn.close()
            return
    conn.execute(
        "INSERT INTO active_log(username, ip, method, path, start_ts, end_ts, count) VALUES(?,?,?,?,?,?,1)",
        (username, ip, method, path, now, now),
    )
    # 维持封顶：删掉超出 MAX_ACTIVE_LOG 的最旧记录。
    conn.execute(
        "DELETE FROM active_log WHERE id <= (SELECT MAX(id) FROM active_log) - ?",
        (MAX_ACTIVE_LOG,),
    )
    conn.commit()
    conn.close()


def last_seen_per_user() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT u.username, "
        "(SELECT MAX(a.end_ts) FROM active_log a WHERE a.username=u.username) AS last_ts "
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
        f"SELECT username, ip, method, path, start_ts, end_ts, count FROM active_log {where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()
    conn.close()
    return {"total": total, "rows": [dict(r) for r in rows]}


# ---------- metrics（机器监控） ----------

def insert_metric(
    ts: int,
    cpu_pct: float,
    mem_pct: float,
    mem_used: int,
    mem_total: int,
    disk_pct: float,
    disk_used: int,
    disk_total: int,
) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO metrics"
        "(ts, cpu_pct, mem_pct, mem_used, mem_total, disk_pct, disk_used, disk_total) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (ts, cpu_pct, mem_pct, mem_used, mem_total, disk_pct, disk_used, disk_total),
    )
    conn.commit()
    conn.close()


def prune_metrics(now_ts: int, retention_days: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM metrics WHERE ts < ?", (now_ts - retention_days * 86400,))
    conn.commit()
    conn.close()


def query_metrics(since_ts: int, window_seconds: int) -> dict:
    """取 [since_ts, now] 窗口内的采样点，按桶取均值降采样到 ≤METRICS_MAX_POINTS 个点。

    桶宽 = max(采样间隔, ceil(窗口/目标点数)) 向上取整到采样间隔的整数倍；
    短窗（桶宽落回采样间隔）等于原量返回。current 为最近一条原始采样，供标题读绝对值。
    """
    bucket = max(METRICS_INTERVAL, math.ceil(window_seconds / METRICS_MAX_POINTS))
    bucket = math.ceil(bucket / METRICS_INTERVAL) * METRICS_INTERVAL
    conn = get_conn()
    rows = conn.execute(
        "SELECT (ts/?)*? AS bts, "
        "AVG(cpu_pct) AS cpu, AVG(mem_pct) AS mem_pct, "
        "AVG(mem_used) AS mem_used, MAX(mem_total) AS mem_total, "
        "AVG(disk_pct) AS disk_pct, AVG(disk_used) AS disk_used, MAX(disk_total) AS disk_total "
        "FROM metrics WHERE ts >= ? GROUP BY ts/? ORDER BY bts",
        (bucket, bucket, since_ts, bucket),
    ).fetchall()
    current = conn.execute(
        "SELECT * FROM metrics ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    conn.close()
    points = [
        {
            "ts": int(r["bts"]),
            "cpu": round(r["cpu"], 2),
            "mem_pct": round(r["mem_pct"], 2),
            "mem_used": int(r["mem_used"]),
            "mem_total": int(r["mem_total"]),
            "disk_pct": round(r["disk_pct"], 2),
            "disk_used": int(r["disk_used"]),
            "disk_total": int(r["disk_total"]),
        }
        for r in rows
    ]
    return {"points": points, "current": dict(current) if current else None, "bucket": bucket}
