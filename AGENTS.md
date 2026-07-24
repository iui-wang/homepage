# AGENTS.md

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
tail -f /var/log/homepage-web.log   # systemd 重定向的 stdout/stderr（见 deploy/homepage.service）
tail -f web/logs/web.log            # logger.py 的结构化日志（每日滚动、保留 14 天）
tail -f logs/auth.log               # 登录失败记录（路径由 config.toml 的 paths.auth_log 决定）
```

依赖安装：
```bash
/home/wyw/miniconda3/envs/homepage/bin/pip install flask flask-sock requests websocket-client
```

## 架构

```
web/app.py      Flask 入口：路由、鉴权装饰器、HTTP/WS 反向代理
web/db.py       SQLite 读写（无 ORM，直接 sqlite3）
web/logger.py   单例 logger：每日午夜滚动、保留 14 天，日志写在 web/logs/（绝对路径，与 CWD 无关），可用环境变量 LOG_DIR 覆盖
web/templates/  login.html（登录页）、index.html（SPA 主页）、错误页
web/static/     app.js（所有前端逻辑）、style.css
config.toml     启动配置（secret_key、admin 密码、监听、路径）—— 已 gitignore
data/homepage.db  SQLite，首次启动自动建库种数据
logs/auth.log   登录失败记录，供 fail2ban 读取
deploy/         systemd 单元与 fail2ban 配置；除 homepage 自身外，还存放接入本门户的其他服务的单元文件（code-server、ttyd、filebrowser、garage-webui）
```

### 关键设计点

- **路由分发**：catch-all `/<path:fullpath>` 按首段 key 查 SQLite routes 表，找不到返回 404。
- **strip_prefix**：routes 表有 `strip_prefix` 字段。普通路由原路径透传（`/key/a/b` → 上游 `/key/a/b`）；`strip_prefix=1` 的路由剥掉前缀（`/key/a/b` → 上游 `/a/b`），用于 code-server 等需要根路径的服务。
- **WebSocket 反代**：`flask-sock` 接浏览器 WS，`websocket-client` 连上游，双向 pump 在两个线程里跑。HTTP 和 WS 在同一前缀共存，Werkzeug 按 Upgrade 头分流。
- **身份头注入（auth proxy）**：HTTP 与 WS 转发时先剥掉客户端带来的 `X-Forwarded-User` / `X-Forwarded-User-Id` / `X-Forwarded-Admin`（防伪造，见 `IDENTITY_HEADER_NAMES`），再注入当前登录用户的真实值（`identity_headers()`）。上游服务（如 timetable 协作日历）只监听内网、唯一入口是本门户，因此可直接信任这三个头识别用户，无需自建登录。
- **Location 改写**：上游返回 `Location: http://127.0.0.1:port/...` 时，自动改写成相对路径，避免内网地址泄露给浏览器。
- **活跃记录**：过滤静态资源和 `/api/`、`/static/` 前缀，只记录有意义请求，且只存路径首段（`/key`）。合并键为 username+path+ip+method 四元组完全相同；以最新一条的 `start_ts` 为基准，距今 ≤60 秒则并入（累加 `count`、推进 `end_ts`），否则新开一条——故单条记录最长只覆盖 60 秒窗口，不会被持续活跃无限拉长。封顶 10 万条（超出删最旧）。
- **会话时长**：`_SESSION_DAYS` 全局变量，从 settings 表读取，`/api/settings` 改后立即生效（无需重启）。
- **机器监控**：守护线程 `_sampler_loop` 每 10 秒（`METRICS_INTERVAL`）直接读 `/proc/stat`、`/proc/meminfo`、`statvfs('/')`（刻意不依赖 psutil，依赖列表保持不变）采 CPU/内存/磁盘占用写入 `metrics` 表，每小时清一次过期数据（保留 7 天）。debug 模式靠 `WERKZEUG_RUN_MAIN` 判重避免重载子进程双采。`/api/metrics?window=1h` 按桶取均值把曲线降采样到 ≤400 点返回；仅 admin 可见（顶栏 📈 弹窗，前端 canvas 手绘，无图表库）。
- **admin 密码**：以 `config.toml` 的 `admin_initial_password` 为唯一来源，每次启动 `init_db` 都同步覆盖到数据库；admin 不能通过 UI/API 改密（接口返回 403），改 config.toml 后重启服务生效。非 admin 用户在「我的」改密，新密码须超 8 位且含大小写、数字、特殊符号。

### SQLite 表结构

| 表 | 作用 |
|---|---|
| `users` | 用户账号，`is_admin=1` 为超级管理员 |
| `routes` | 路由配置（key、上游 host/port、strip_prefix、sort_order）|
| `user_routes` | 非 admin 用户可见路由的多对多关联 |
| `settings` | KV 配置（目前只有 `session_days`）|
| `active_log` | 请求记录（username、ip、method、path、start_ts、end_ts、count）；60 秒窗口聚合，启动时若检测到旧 `ts` 单列结构会 drop 重建并清空历史 |
| `metrics` | 机器监控采样（ts 主键、cpu/mem/disk 的 pct 与 used/total 字节）；每 10 秒一条，保留 7 天 |

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
- 日志文件写在 `web/logs/` 下（`.gitignore` 的 `logs/` 规则已覆盖）。
- 新增路由的 key 不能与 `RESERVED_ROUTE_KEYS`（`api`、`static`、`login`、`logout`、`favicon.ico`、`health`）冲突。
- 上游原路径透传：`{IP}/bill/x` → 上游 `/bill/x`，上游服务需在相同前缀下提供服务。
- `routes_repo/` 目前是**半成品**：`app.py` 末尾保留了 `sys.modules["app"] = sys.modules["__main__"]` 与注释里提到的 `setup_routes_repo()`，但该函数尚未在代码中实现、目录内容（如 `dice3d/`）也未被任何加载器接入。看到那段注释别去找不存在的实现——要么补上加载逻辑，要么连同别名一起清掉。
  - **启动阻断坑（已修）**：那行 `sys.modules[...]` 在 `if __name__ == "__main__"` 块里、`main()` 之前执行，曾因缺 `import sys` 抛 `NameError` 导致 `python web/app.py` 启动即崩。现 `app.py` 顶部已补 `import sys`，启动不再被阻断。但别名本身仍是无效遗留（routes_repo 未接入），可连同那段注释一并删除，无副作用。
- 根目录散落的 `web.log*` 文件未被 gitignore 覆盖（`.gitignore` 只匹配 `web/*.log*`），是 `LOG_DIR` 改成绝对路径前、以根目录为 CWD 跑出来的遗留物，可安全删除。

## 操作文档

- **接入新服务**：优先用项目技能 `add-site-to-homepage`（`.claude/skills/`）；详细步骤见 [doc/add-site.md](doc/add-site.md)（含代理模式选择、curl 命令、权限分配、故障排查）

## 验证方式

无测试套件、无 lint。改动后用 `--debug` 模式跑起来，curl 验证路由/API，观察 `journalctl -u homepage -f` 与 `web/logs/web.log`。
