"""homepage —— 反向代理路由门户 + 用户/路由管理 + 活跃记录。

- 监听 0.0.0.0:80，是唯一公网入口。
- /<machine>/<key>/... 反向代理到该路由配置的上游 ip:port（strip_prefix=0 时原路径透传）。
- 登录失败写 auth.log（含 IP），配合 fail2ban 封禁暴力破解。
"""

import argparse
import ipaddress
import os
import re
import sys
import threading
import time
import tomllib
from collections import deque
from datetime import datetime, timedelta
from functools import wraps

import requests
import websocket as wsclient
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sock import Sock
from werkzeug.security import check_password_hash, generate_password_hash

import db
from logger import get_logger

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 80
DEFAULT_CONFIG = "/home/wyw/homepage/config.toml"

# 活跃记录前端分页：每页条数。
PER_PAGE = 10

# 不计入活跃记录的静态资源后缀。
STATIC_EXT = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".map", ".webp",
)

# 反向代理时需要剔除的逐跳（hop-by-hop）头。
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# 转发给上游的登录用户身份头（auth proxy 模式）。上游只监听内网、唯一入口是
# homepage，因此可信任这些头；客户端带来的同名头一律剥掉，防伪造。
IDENTITY_HEADER_NAMES = {"x-forwarded-user", "x-forwarded-user-id", "x-forwarded-admin"}


def identity_headers(user: dict) -> dict:
    return {
        "X-Forwarded-User": user["username"],
        "X-Forwarded-User-Id": str(user["id"]),
        "X-Forwarded-Admin": "1" if user["is_admin"] else "0",
    }

# 反代连接超时与单次读取超时（秒）。响应体采用流式转发，下载大文件时不会因为
# 整个传输超过此时长而中断；仅在连续 5 分钟收不到上游数据时才判定为超时。
PROXY_CONNECT_TIMEOUT = 5
PROXY_READ_TIMEOUT = 300

# 机器监控：采样间隔（秒）、数据保留天数、可选时间窗（key -> 秒）。
METRICS_INTERVAL = 10
METRICS_RETENTION_DAYS = 7
METRICS_WINDOWS = {
    "1m": 60, "10m": 600, "30m": 1800, "1h": 3600,
    "3h": 10800, "1d": 86400, "3d": 259200, "7d": 604800,
}

# 卡片点击排序：滑动窗口时长（秒，72 小时）。点击记录存纯内存、不落库，
# 进程重启后清零，卡片顺序恢复 routes.sort_order 次序。
CLICK_WINDOW_SECONDS = 72 * 3600

logger = get_logger("web")

app = Flask(__name__)

# WebSocket 反代：让底层 simple_websocket 协商 ttyd 使用的 `tty` 子协议，并定期 ping 保活。
app.config["SOCK_SERVER_OPTIONS"] = {"subprotocols": ["tty"], "ping_interval": 25}
sock = Sock(app)

_AUTH_LOG_PATH = "/home/wyw/homepage/logs/auth.log"
_SESSION_DAYS = db.DEFAULT_SESSION_DAYS

# Homepage 只监听回环地址，唯一允许代传客户端地址的前置代理是本机 Caddy。
# 不可对任意来源盲目信任 X-Forwarded-For，否则攻击者可伪造 IP 绕过 fail2ban。
TRUSTED_PROXY_IPS = {"127.0.0.1", "::1"}


# ---------------- 密码与鉴权工具 ----------------

def client_ip() -> str:
    """返回真实客户端 IP；仅信任本机 Caddy 注入的 X-Forwarded-For。"""
    peer_ip = request.remote_addr or "-"
    if peer_ip not in TRUSTED_PROXY_IPS:
        return peer_ip
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    candidate = forwarded_for.split(",", 1)[0].strip()
    if not candidate:
        return peer_ip
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        logger.warning("忽略来自可信代理的非法 X-Forwarded-For：%r", forwarded_for)
        return peer_ip

def password_ok(pw: str) -> bool:
    """改密复杂度：超过 8 位，且含大小写字母、数字、特殊符号。"""
    return bool(
        len(pw) > 8
        and re.search(r"[a-z]", pw)
        and re.search(r"[A-Z]", pw)
        and re.search(r"\d", pw)
        and re.search(r"[^A-Za-z0-9]", pw)
    )


PASSWORD_RULE_MSG = "密码必须超过 8 位，且同时包含大写字母、小写字母、数字和特殊符号。"

LOGIN_FAIL_MSG = (
    "用户名或密码错误。注意：1 小时内累计 5 次登录失败，你的 IP 将被封禁 24 小时（fail2ban）。"
)


def log_auth_failure(ip: str, username: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} LOGIN FAILED ip={ip} user={username}\n"
    with open(_AUTH_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)


def current_user() -> dict | None:
    if "user_id" not in session:
        return None
    return {
        "id": session["user_id"],
        "username": session["username"],
        "is_admin": session["is_admin"],
    }


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login", next=request.full_path))
        g.user = user
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not g.user["is_admin"]:
            if request.path.startswith("/api/"):
                return jsonify({"error": "需要管理员权限"}), 403
            return Response(render_template("forbidden.html"), status=403)
        return view(*args, **kwargs)

    return wrapper


def maybe_log_active(username: str) -> None:
    """记录一次有意义的请求（过滤静态资源与 homepage 自身接口）。"""
    p = request.path
    if p.startswith("/api/") or p.startswith("/static/"):
        return
    if p in ("/login", "/logout", "/health", "/favicon.ico"):
        return
    if p.lower().endswith(STATIC_EXT):
        return
    # URL 是 /<machine>/<key>/... 两级结构，记录前两段以保留服务信息。
    segs = "/" + "/".join(p.lstrip("/").split("/")[:2])
    db.log_active(username, client_ip(), request.method, segs)


# ---------------- 机器监控采样 ----------------

def _read_cpu_times() -> tuple[int, int]:
    """读 /proc/stat 第一行，返回 (total_jiffies, idle_jiffies)；idle 含 iowait。"""
    with open("/proc/stat", encoding="ascii") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return sum(vals), idle


def _read_mem() -> tuple[int, int]:
    """读 /proc/meminfo，返回 (used_bytes, total_bytes)；used = total - MemAvailable。"""
    info = {}
    with open("/proc/meminfo", encoding="ascii") as f:
        for line in f:
            k, _, rest = line.partition(":")
            info[k] = int(rest.split()[0]) * 1024  # kB -> bytes
    total = info["MemTotal"]
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    return total - avail, total


def _read_disk() -> tuple[int, int]:
    """statvfs('/')，返回 (used_bytes, total_bytes)；used = total - free。"""
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    return total - free, total


def _sampler_loop() -> None:
    """每 METRICS_INTERVAL 秒采一次写库，约每小时清一次过期数据。守护线程运行。"""
    prev_total, prev_idle = _read_cpu_times()
    prune_every = max(1, 3600 // METRICS_INTERVAL)
    i = 0
    while True:
        time.sleep(METRICS_INTERVAL)
        try:
            total, idle = _read_cpu_times()
            dt, di = total - prev_total, idle - prev_idle
            prev_total, prev_idle = total, idle
            cpu_pct = max(0.0, min(100.0, (1 - di / dt) * 100)) if dt > 0 else 0.0
            mem_used, mem_total = _read_mem()
            disk_used, disk_total = _read_disk()
            now = int(time.time())
            db.insert_metric(
                now, cpu_pct,
                mem_used / mem_total * 100, mem_used, mem_total,
                disk_used / disk_total * 100, disk_used, disk_total,
            )
            i += 1
            if i % prune_every == 0:
                db.prune_metrics(now, METRICS_RETENTION_DAYS)
        except Exception:
            logger.exception("机器监控采样失败")


def start_sampler(debug: bool) -> None:
    """启动采样守护线程。debug 模式下 Werkzeug 会 fork 出重载子进程，
    只在真正服务的进程（WERKZEUG_RUN_MAIN=true）里启，避免双采。"""
    if debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    threading.Thread(target=_sampler_loop, daemon=True, name="metrics-sampler").start()
    logger.info("机器监控采样线程已启动 interval=%ss", METRICS_INTERVAL)


# ---------------- 会话时长 ----------------

@app.before_request
def _make_session_permanent():
    session.permanent = True


# ---------------- 登录 / 登出 ----------------

@app.route("/health")
def health():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if current_user() is not None:
            return redirect(url_for("index"))
        return render_template("login.html")

    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ip = client_ip()

    user = db.get_user(username)
    if user is None or not check_password_hash(user["password_hash"], password):
        log_auth_failure(ip, username or "<empty>")
        logger.info("登录失败 ip=%s user=%s", ip, username)
        return jsonify({"ok": False, "error": LOGIN_FAIL_MSG}), 401

    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["is_admin"] = bool(user["is_admin"])
    logger.info("登录成功 ip=%s user=%s", ip, username)
    return jsonify({"ok": True})


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- 首页（SPA） ----------------

@app.route("/")
@login_required
def index():
    maybe_log_active(g.user["username"])
    return render_template(
        "index.html",
        username=g.user["username"],
        is_admin=g.user["is_admin"],
    )


# ---------------- 卡片点击排序 ----------------

# 内存点击记录：username -> key -> 点击时间戳队列（滑动窗口见 CLICK_WINDOW_SECONDS）。
# 请求线程与 WS pump 线程并发访问，统一走 _CLICKS_LOCK。
_CLICKS: dict[str, dict[str, deque]] = {}
_CLICKS_LOCK = threading.Lock()


def record_click(username: str, key: str) -> None:
    """记录一次卡片点击（时间戳入队，过期数据在读取时 prune）。"""
    with _CLICKS_LOCK:
        _CLICKS.setdefault(username, {}).setdefault(key, deque()).append(time.time())


def click_counts(username: str) -> dict[str, int]:
    """返回该用户各 key 在窗口内的点击次数，顺带 prune 过期时间戳。"""
    cutoff = time.time() - CLICK_WINDOW_SECONDS
    counts: dict[str, int] = {}
    with _CLICKS_LOCK:
        for key, dq in _CLICKS.get(username, {}).items():
            while dq and dq[0] < cutoff:
                dq.popleft()
            if dq:
                counts[key] = len(dq)
    return counts


# ---------------- 自身 API ----------------

@app.route("/api/me")
@login_required
def api_me():
    routes = db.visible_routes_for(g.user["id"], g.user["is_admin"])
    # 排序主关键字是窗口内点击数；list.sort 稳定排序，点击数相同时保持
    # visible_routes_for 的原顺序（即 routes.sort_order），成为次关键字。
    counts = click_counts(g.user["username"])
    for r in routes:
        r["clicks"] = counts.get(r["key"], 0)
    routes.sort(key=lambda r: -r["clicks"])
    return jsonify(
        {
            "username": g.user["username"],
            "is_admin": g.user["is_admin"],
            "routes": routes,
        }
    )


@app.route("/api/track-click", methods=["POST"])
@login_required
def api_track_click():
    """前端卡片点击上报。只接受当前用户可见的路由 key，防乱刷。"""
    data = request.get_json(silent=True) or {}
    key = data.get("key") or ""
    visible = {r["key"] for r in db.visible_routes_for(g.user["id"], g.user["is_admin"])}
    if key not in visible:
        return jsonify({"error": "未知路由"}), 400
    record_click(g.user["username"], key)
    return jsonify({"ok": True})


@app.route("/api/change-password", methods=["POST"])
@login_required
def api_change_password():
    if g.user["is_admin"]:
        return jsonify({"error": "admin 密码通过 config.toml 管理，修改后重启服务生效"}), 403
    data = request.get_json(silent=True) or {}
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""

    user = db.get_user(g.user["username"])
    if user is None or not check_password_hash(user["password_hash"], old):
        return jsonify({"error": "原密码不正确"}), 400
    if not password_ok(new):
        return jsonify({"error": PASSWORD_RULE_MSG}), 400
    db.update_password(g.user["id"], generate_password_hash(new))
    return jsonify({"ok": True})


# ----- 用户管理（admin） -----

@app.route("/api/users")
@admin_required
def api_list_users():
    return jsonify({"users": db.list_users(), "routes": db.list_routes()})


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", username):
        return jsonify({"error": "用户名需为 2~32 位字母/数字/._- 组合"}), 400
    if username == "admin" or db.get_user(username) is not None:
        return jsonify({"error": "用户名已存在"}), 400
    if not password_ok(password):
        return jsonify({"error": PASSWORD_RULE_MSG}), 400
    db.create_user(username, generate_password_hash(password))
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_delete_user(user_id):
    target = next((u for u in db.list_users() if u["id"] == user_id), None)
    if target is None:
        return jsonify({"error": "用户不存在"}), 404
    if target["is_admin"]:
        return jsonify({"error": "不能删除 admin"}), 400
    db.delete_user(user_id)
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>/routes", methods=["POST"])
@admin_required
def api_set_user_routes(user_id):
    target = next((u for u in db.list_users() if u["id"] == user_id), None)
    if target is None:
        return jsonify({"error": "用户不存在"}), 404
    if target["is_admin"]:
        return jsonify({"error": "admin 恒有全部路由，无需设置"}), 400
    data = request.get_json(silent=True) or {}
    keys = data.get("routes") or []
    db.set_user_routes(user_id, keys)
    return jsonify({"ok": True})


# ----- 路由 / 配置（admin） -----

@app.route("/api/routes")
@admin_required
def api_list_routes():
    return jsonify({"routes": db.list_routes(), "machines": db.list_machines()})


def _validate_route_payload(data, *, require_key):
    machine = (data.get("machine") or "").strip()
    key = (data.get("key") or "").strip()
    name = (data.get("display_name") or "").strip()
    host = (data.get("upstream_host") or "").strip()
    port = data.get("upstream_port")
    strip_prefix = bool(data.get("strip_prefix", False))
    if not db.machine_exists(machine):
        return None, "machine 必须是 machines 表里已注册的 slug"
    if require_key:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", key):
            return None, "路由 key 需为 1~40 位字母/数字/_-"
        if key in db.RESERVED_ROUTE_KEYS:
            return None, f"路由 key 不能使用保留字：{key}"
    if not name:
        return None, "显示名不能为空"
    if not host:
        return None, "上游 host 不能为空"
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None, "上游 port 必须是整数"
    if not (1 <= port <= 65535):
        return None, "上游 port 取值 1~65535"
    return {"machine": machine, "key": key, "display_name": name, "upstream_host": host, "upstream_port": port, "strip_prefix": strip_prefix}, None


@app.route("/api/routes", methods=["POST"])
@admin_required
def api_add_route():
    data = request.get_json(silent=True) or {}
    payload, err = _validate_route_payload(data, require_key=True)
    if err:
        return jsonify({"error": err}), 400
    if db.get_route(payload["machine"], payload["key"]) is not None:
        return jsonify({"error": "该机器上路由 key 已存在"}), 400
    db.add_route(payload["machine"], payload["key"], payload["display_name"], payload["upstream_host"], payload["upstream_port"], payload["strip_prefix"])
    return jsonify({"ok": True})


@app.route("/api/routes/<int:route_id>", methods=["PUT"])
@admin_required
def api_update_route(route_id):
    data = request.get_json(silent=True) or {}
    payload, err = _validate_route_payload(data, require_key=False)
    if err:
        return jsonify({"error": err}), 400
    db.update_route(route_id, payload["machine"], payload["display_name"], payload["upstream_host"], payload["upstream_port"], payload["strip_prefix"])
    return jsonify({"ok": True})


@app.route("/api/routes/<int:route_id>", methods=["DELETE"])
@admin_required
def api_delete_route(route_id):
    db.delete_route(route_id)
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "PUT"])
@admin_required
def api_settings():
    global _SESSION_DAYS
    if request.method == "GET":
        return jsonify({"session_days": db.get_session_days()})
    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("session_days"))
    except (TypeError, ValueError):
        return jsonify({"error": "会话时长必须是整数（天）"}), 400
    if not (1 <= days <= 3650):
        return jsonify({"error": "会话时长取值 1~3650 天"}), 400
    db.set_session_days(days)
    _SESSION_DAYS = days
    app.permanent_session_lifetime = timedelta(days=days)
    return jsonify({"ok": True})


# ----- 活跃记录（admin） -----

@app.route("/api/active/last")
@admin_required
def api_active_last():
    return jsonify({"users": db.last_seen_per_user()})


@app.route("/api/active/log")
@admin_required
def api_active_log():
    username = request.args.get("username") or None
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    result = db.query_active_log(username, page, PER_PAGE)
    return jsonify(
        {
            "rows": result["rows"],
            "total": result["total"],
            "page": page,
            "per_page": PER_PAGE,
        }
    )


# ----- 机器监控（admin） -----

@app.route("/api/metrics")
@admin_required
def api_metrics():
    window = request.args.get("window", "1h")
    seconds = METRICS_WINDOWS.get(window)
    if seconds is None:
        return jsonify({"error": "无效时间窗"}), 400
    now = int(time.time())
    return jsonify(db.query_metrics(now - seconds, seconds))


# ---------------- 反向代理（catch-all） ----------------

@app.route("/<path:fullpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@login_required
def proxy(fullpath):
    # URL 两级结构 /<machine>/<key>/<rest>：首段机器 slug，次段服务 key。
    parts = fullpath.split("/")
    if len(parts) < 2:
        return Response(render_template("notfound.html"), status=404)
    machine, key = parts[0], parts[1]
    route = db.get_route(machine, key)
    if route is None:
        return Response(render_template("notfound.html"), status=404)

    if not db.user_can_see_route(g.user["id"], g.user["is_admin"], key):
        return Response(render_template("forbidden.html", route=key), status=403)

    maybe_log_active(g.user["username"])

    # strip_prefix 路由（如 code-server）剥掉 /<machine>/<key> 两段前缀再转发；
    # 普通路由完整原路径透传（上游应用挂在 /<machine>/<key> 前缀下）。
    path = request.path
    if route["strip_prefix"]:
        path = path[len(machine) + len(key) + 2:] or "/"

    target = f"http://{route['upstream_host']}:{route['upstream_port']}{path}"
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", "ignore")

    fwd_headers = {
        k: v
        for k, v in request.headers
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in ("host", "content-length")
        and k.lower() not in IDENTITY_HEADER_NAMES
    }
    fwd_headers.update(identity_headers(g.user))

    try:
        upstream = requests.request(
            request.method,
            target,
            headers=fwd_headers,
            data=request.get_data(),
            allow_redirects=False,
            timeout=(PROXY_CONNECT_TIMEOUT, PROXY_READ_TIMEOUT),
            stream=True,
        )
    except requests.RequestException as exc:
        logger.warning("上游不可达 route=%s/%s target=%s err=%s", machine, key, target, exc)
        return Response(
            render_template("upstream_error.html", route=route["display_name"]),
            status=502,
        )

    # requests 会自动解压带 Content-Encoding 的响应，此时保留上游的
    # Content-Length 会与解压后的实体不一致；未压缩响应则必须透传长度。
    # 例如豚鼠小屋的 BGM 加载条依赖 Content-Length 来计算下载进度。
    excluded = HOP_BY_HOP | {"content-encoding"}
    upstream_base = f"http://{route['upstream_host']}:{route['upstream_port']}"
    out_headers = []
    for k, v in upstream.headers.items():
        header_name = k.lower()
        if header_name in excluded:
            continue
        if header_name == "content-length" and "content-encoding" in upstream.headers:
            continue
        # 上游若返回指向自身内网地址的重定向（如 Werkzeug 严格斜杠 308），改写成
        # 相对路径，避免把 127.0.0.1:端口 这种内网地址泄露给外部浏览器。
        if k.lower() == "location" and v.startswith(upstream_base):
            v = v[len(upstream_base):] or "/"
        out_headers.append((k, v))
    # 不要读取 upstream.content：那会让 HTTPS 门户先完整缓存附件，客户端一直
    # 收不到首字节，也会把慢下载卡在固定超时里。逐块转发可保留 Range 的 206 /
    # Content-Range 语义，手机的下载管理器才可在锁屏或断网后从已收位置续传。
    def stream_upstream():
        try:
            yield from upstream.iter_content(chunk_size=64 * 1024)
        finally:
            upstream.close()

    return Response(
        stream_upstream(),
        status=upstream.status_code,
        headers=out_headers,
        direct_passthrough=True,
    )


# ---------------- WebSocket 反向代理 ----------------

# ttyd 等服务的终端 I/O 全走 WebSocket，上面基于 requests 的 catch-all 无法转发 Upgrade。
# 这里用 flask-sock 接收浏览器侧 WS（websocket=True 路由，与上面的 HTTP catch-all 同前缀共存，
# Werkzeug 按是否为 Upgrade 请求分流），再用 websocket-client 连上游 ws://host:port/<原路径>，
# 两端字节对拷并保留 text/binary 帧型。登录门、路由可见性校验与 HTTP 代理保持一致。
@sock.route("/<path:fullpath>")
def ws_proxy(ws, fullpath):
    user = current_user()
    if user is None:
        return
    parts = fullpath.split("/")
    if len(parts) < 2:
        return
    machine, key = parts[0], parts[1]
    route = db.get_route(machine, key)
    if route is None or not db.user_can_see_route(user["id"], user["is_admin"], key):
        return

    ws_path = request.path
    if route["strip_prefix"]:
        ws_path = ws_path[len(machine) + len(key) + 2:] or "/"

    target = f"ws://{route['upstream_host']}:{route['upstream_port']}{ws_path}"
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", "ignore")

    # 透传客户端请求的子协议，兼容 ttyd（tty）和 code-server（无固定子协议）。
    proto_header = request.headers.get("Sec-WebSocket-Protocol", "")
    client_protocols = [p.strip() for p in proto_header.split(",") if p.strip()]

    try:
        upstream = wsclient.create_connection(
            target,
            subprotocols=client_protocols or None,
            timeout=10,
            header=identity_headers(user),
        )
    except Exception as exc:
        logger.warning("WS 上游连接失败 route=%s/%s target=%s err=%s", machine, key, target, exc)
        return
    upstream.settimeout(None)

    stop = threading.Event()

    def upstream_to_client():
        try:
            while not stop.is_set():
                opcode, data = upstream.recv_data()
                if opcode == wsclient.ABNF.OPCODE_TEXT:
                    ws.send(data.decode("utf-8", "replace"))
                elif opcode == wsclient.ABNF.OPCODE_BINARY:
                    ws.send(data)
                elif opcode == wsclient.ABNF.OPCODE_CLOSE:
                    break
        except Exception:
            pass
        finally:
            stop.set()
            try:
                ws.close()
            except Exception:
                pass

    pump = threading.Thread(target=upstream_to_client, daemon=True)
    pump.start()

    try:
        while not stop.is_set():
            msg = ws.receive()
            if msg is None:
                break
            if isinstance(msg, str):
                upstream.send(msg, wsclient.ABNF.OPCODE_TEXT)
            else:
                upstream.send(msg, wsclient.ABNF.OPCODE_BINARY)
    except Exception:
        pass
    finally:
        stop.set()
        try:
            upstream.close()
        except Exception:
            pass


# ---------------- 启动 ----------------

def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。default=%(default)s")
    parser.add_argument("--host", default=None, help="监听地址，覆盖配置。default=配置中的值")
    parser.add_argument("--port", type=int, default=None, help="端口，覆盖配置。default=配置中的值")
    parser.add_argument("--debug", action="store_true", help="开启 Flask debug")
    return parser.parse_args()


def main() -> None:
    global _AUTH_LOG_PATH, _SESSION_DAYS
    args = parse_args()
    config = load_config(args.config)

    db.configure(config["paths"]["db"])
    db.init_db(config["security"]["admin_initial_password"])

    _AUTH_LOG_PATH = config["paths"]["auth_log"]
    _SESSION_DAYS = db.get_session_days()
    app.permanent_session_lifetime = timedelta(days=_SESSION_DAYS)
    app.secret_key = config["security"]["secret_key"]

    host = args.host or config["server"]["host"]
    port = args.port or config["server"]["port"]
    start_sampler(args.debug)
    logger.info("homepage 启动 host=%s port=%s session_days=%s", host, port, _SESSION_DAYS)
    app.run(host=host, port=port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    # 本文件以 `python app.py` 启动时身份是 __main__。setup_routes_repo() 动态加载的
    # routes_repo 子模块若 import app，会把本文件重新加载成名为 'app' 的第二份模块，
    # 导致顶层副作用（如 get_logger）重复执行、logger handler 累积、日志被成倍放大。
    # 把 'app' 别名到 __main__，让这些子模块复用同一份模块，避免重复加载。
    sys.modules["app"] = sys.modules["__main__"]
    logger = get_logger("web")
    try:
        main()
    except Exception:
        logger.exception("Unhandled exception, exiting")
        raise
