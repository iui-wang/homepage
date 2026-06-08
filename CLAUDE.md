# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

反向代理路由门户。监听 `0.0.0.0:80`，是本机唯一公网入口。Flask 后端 + 原生 JS 前端（SPA）。

## 启动与运行

```bash
# 直接运行（conda 环境 homepage）
/home/wyw/miniconda3/envs/homepage/bin/python web/app.py --config config.toml

# 调试模式
/home/wyw/miniconda3/envs/homepage/bin/python web/app.py --config config.toml --debug

# systemd 管理
systemctl status homepage
systemctl restart homepage
journalctl -u homepage -f

# 查看实时日志
tail -f /var/log/homepage-web.log
tail -f logs/auth.log
```

依赖安装：
```bash
/home/wyw/miniconda3/envs/homepage/bin/pip install flask flask-sock requests websocket-client
```

## 架构

```
web/app.py      Flask 入口：路由、鉴权装饰器、HTTP/WS 反向代理
web/db.py       SQLite 读写（无 ORM，直接 sqlite3）
web/logger.py   按日期滚动日志，日志文件写在 WorkingDirectory（web/）下
web/templates/  login.html（登录页）、index.html（SPA 主页）、错误页
web/static/     app.js（所有前端逻辑）、style.css
config.toml     启动配置（secret_key、admin 初密、监听、路径）—— 已 gitignore
data/homepage.db  SQLite，首次启动自动建库种数据
logs/auth.log   登录失败记录，供 fail2ban 读取
```

### 关键设计点

- **路由分发**：catch-all `/<path:fullpath>` 按首段 key 查 SQLite routes 表，找不到返回 404。
- **strip_prefix**：routes 表有 `strip_prefix` 字段。普通路由原路径透传（`/key/a/b` → 上游 `/key/a/b`）；`strip_prefix=1` 的路由剥掉前缀（`/key/a/b` → 上游 `/a/b`），用于 code-server 等需要根路径的服务。
- **WebSocket 反代**：`flask-sock` 接浏览器 WS，`websocket-client` 连上游，双向 pump 在两个线程里跑。HTTP 和 WS 在同一前缀共存，Werkzeug 按 Upgrade 头分流。
- **Location 改写**：上游返回 `Location: http://127.0.0.1:port/...` 时，自动改写成相对路径，避免内网地址泄露给浏览器。
- **活跃记录**：过滤静态资源和 `/api/`、`/static/` 前缀，只记录有意义请求，封顶 10 万条（超出删最旧）。
- **会话时长**：`_SESSION_DAYS` 全局变量，从 settings 表读取，`/api/settings` 改后立即生效（无需重启）。

### SQLite 表结构

| 表 | 作用 |
|---|---|
| `users` | 用户账号，`is_admin=1` 为超级管理员 |
| `routes` | 路由配置（key、上游 host/port、strip_prefix、sort_order）|
| `user_routes` | 非 admin 用户可见路由的多对多关联 |
| `settings` | KV 配置（目前只有 `session_days`）|
| `active_log` | 请求记录（username、ip、method、path、ts）|

admin 恒有所有路由权限，不走 user_routes 表。

## fail2ban 配置

```bash
deploy/homepage-auth.conf  → /etc/fail2ban/filter.d/
deploy/homepage-jail.local → /etc/fail2ban/jail.d/homepage.local
systemctl restart fail2ban
```

规则：1 小时内登录失败 5 次 → 封 IP 24 小时（80 端口）。

## 注意事项

- `config.toml` 已 gitignore，修改后不会自动提交。参考 `config.toml.example`。
- `data/` 和 `logs/` 目录已 gitignore。
- 日志文件（`web/web.log*`）写在 `web/` 目录下，也已 gitignore。
- 新增路由的 key 不能与 `RESERVED_ROUTE_KEYS`（`api`、`static`、`login`、`logout`、`favicon.ico`、`health`）冲突。
- 上游原路径透传：`{IP}/bill/x` → 上游 `/bill/x`，上游服务需在相同前缀下提供服务。

## 操作文档

- **接入新服务**：见 [doc/add-site.md](doc/add-site.md)（含代理模式选择、curl 命令、权限分配、故障排查）
