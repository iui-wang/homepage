# AGENTS.md

本文件是 AI 智能体在本仓库工作时的项目说明，内容与代码保持一致，以此为准。

## 项目简介

反向代理路由门户。监听 `0.0.0.0:80`，是本机唯一公网入口，所有上游服务（只监听 127.0.0.1）都经它暴露。功能：登录门 + 用户/路由管理 + 活跃记录 + 机器监控。Flask 后端 + 原生 JS 前端（SPA），SQLite 存储。

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
web/app.py      Flask 入口：登录/鉴权装饰器、管理 API、HTTP/WS 反向代理、机器监控采样
web/db.py       SQLite schema 与读写（无 ORM，直接 sqlite3）
web/logger.py   单例 logger：每日午夜滚动、保留 14 天，日志写在 web/logs/（绝对路径，与 CWD 无关），可用环境变量 LOG_DIR 覆盖
web/templates/  login.html（登录页）、index.html（SPA 主页）、forbidden/notfound/upstream_error.html（错误页）
web/static/     app.js（所有前端逻辑）、style.css
config.toml     启动配置（secret_key、admin 密码、监听、路径）—— 已 gitignore
data/homepage.db  SQLite，首次启动自动建库种数据（默认路由 xiangyun、bill）
logs/auth.log   登录失败记录，供 fail2ban 读取
deploy/         systemd 单元与 fail2ban 配置；除 homepage 自身外，还存放接入本门户的其他服务的单元文件（code-server、ttyd、filebrowser、garage-webui）
doc/add-site.md 接入新服务的操作文档
```

### 关键设计点

- **路由分发**：URL 是机器+服务两级结构 `/<machine>/<key>/<rest>`。catch-all `/<path:fullpath>` 取前两段，按 (machine, key) 查 SQLite routes 表（唯一约束也是这两列），任一查不到返回 404。机器 slug 固定在 machines 表（tokyo / shanghai / m720q / shin）。旧单段 `/<key>` URL 已废弃，一律 404。路由改动实时生效，无需重启。
- **strip_prefix**：routes 表有 `strip_prefix` 字段。普通路由完整原路径透传（`/tokyo/key/a/b` → 上游 `/tokyo/key/a/b`，上游应用挂在 `/<machine>/<key>` 前缀下）；`strip_prefix=1` 的路由剥掉两段前缀（`/tokyo/key/a/b` → 上游 `/a/b`），用于 code-server 等需要根路径的服务。
- **WebSocket 反代**：`flask-sock` 接浏览器 WS（协商 `tty` 子协议、25 秒 ping 保活），`websocket-client` 连上游，双向 pump 在两个线程里跑，保留 text/binary 帧型。HTTP 和 WS 在同一前缀共存，Werkzeug 按 Upgrade 头分流。
- **身份头注入（auth proxy）**：HTTP 与 WS 转发时先剥掉客户端带来的 `X-Forwarded-User` / `X-Forwarded-User-Id` / `X-Forwarded-Admin`（防伪造，见 `IDENTITY_HEADER_NAMES`），再注入当前登录用户的真实值（`identity_headers()`）。上游服务只监听内网、唯一入口是本门户，因此可直接信任这三个头识别用户，无需自建登录。
- **Location 改写**：上游返回 `Location: http://127.0.0.1:port/...` 时，自动改写成相对路径，避免内网地址泄露给浏览器。
- **活跃记录**：过滤静态资源和 `/api/`、`/static/` 前缀，只记录有意义请求，且只存路径前两段（`/<machine>/<key>`）。合并键为 username+path+ip+method 四元组完全相同；以最新一条的 `start_ts` 为基准，距今 ≤60 秒则并入（累加 `count`、推进 `end_ts`），否则新开一条——故单条记录最长只覆盖 60 秒窗口，不会被持续活跃无限拉长。封顶 10 万条（超出删最旧）。
- **会话时长**：`_SESSION_DAYS` 全局变量，从 settings 表读取，`/api/settings` 改后立即生效（无需重启）。
- **机器监控**：守护线程 `_sampler_loop` 每 10 秒（`METRICS_INTERVAL`）直接读 `/proc/stat`、`/proc/meminfo`、`statvfs('/')`（刻意不依赖 psutil，依赖列表保持不变）采 CPU/内存/磁盘占用写入 `metrics` 表，每小时清一次过期数据（保留 7 天）。debug 模式靠 `WERKZEUG_RUN_MAIN` 判重避免重载子进程双采。`/api/metrics?window=1h`（可选窗口见 `METRICS_WINDOWS`：1m~7d）按桶取均值把曲线降采样到 ≤400 点返回；仅 admin 可见（顶栏 📈 弹窗，前端 canvas 手绘，无图表库）。
- **admin 密码**：以 `config.toml` 的 `admin_initial_password` 为唯一来源，每次启动 `init_db` 都同步覆盖到数据库；admin 不能通过 UI/API 改密（接口返回 403），改 config.toml 后重启服务生效。非 admin 用户在「我的」改密，新密码须超 8 位且含大小写、数字、特殊符号。

### SQLite 表结构

| 表 | 作用 |
|---|---|
| `users` | 用户账号，`is_admin=1` 为超级管理员 |
| `machines` | 机器清单（slug 主键、display_name、sort_order）；URL 第一级路径段。启动时 INSERT OR IGNORE 种入四台 |
| `routes` | 路由配置（machine、key、上游 host/port、strip_prefix、sort_order），唯一约束 (machine, key)。旧库迁移：重建表加 machine 列，再按 upstream_host 回填（127.0.0.1/10.77.0.1→tokyo，10.77.0.2→shanghai，10.77.0.3→shin，10.77.0.6→m720q），幂等 |
| `user_routes` | 非 admin 用户可见路由的多对多关联（授权按服务 key：授予某 key 即授予它在所有机器上的路由行）|
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

- `config.toml` 已 gitignore，修改后不会自动提交。参考 `config.toml.example`（注意：示例里的路径还是旧的 `/root/...`，实际部署在 `/home/wyw/homepage`，以 `web/app.py` 的 `DEFAULT_CONFIG` 和 `deploy/homepage.service` 为准）。
- `data/` 和 `logs/` 目录已 gitignore；`data/homepage.db.bak-*` 是手工备份，不影响运行。
- `README.md` 已过时（还在描述废弃的单段 `/<key>` URL、`/root/miniconda3` 路径，依赖列表缺 flask-sock/websocket-client，且误称 admin 可在「我的」改密）。不要以它为准，以本文件和代码为准。
- 新增路由的 key 不能与 `RESERVED_ROUTE_KEYS`（`api`、`static`、`login`、`logout`、`favicon.ico`、`health`）冲突。
- 上游服务必须绑 `127.0.0.1`，直接对公网监听会绕过登录门。普通模式为完整原路径透传（`{IP}/tokyo/bill/x` → 上游 `/tokyo/bill/x`），上游需在 `/<machine>/<key>` 前缀下提供服务；否则用 `strip_prefix=1`（剥两段前缀，上游收到 `/x`）。
- `routes_repo/` 是**未接入的半成品**：`app.py` 末尾的 `sys.modules["app"] = sys.modules["__main__"]` 及其注释提到的 `setup_routes_repo()` 在代码中并不存在，目录内容（如 `dice3d/`）也没有任何加载器引用。别去找不存在的实现——要么补上加载逻辑，要么把别名和注释一起删掉（无副作用）。
- `web/` 下的 `web.log` 和 `web.log.2026-06-*` 是 logger 改绝对路径前以 `web/` 为 CWD 跑出来的遗留文件，现已被 `.gitignore` 的 `web/*.log*` 覆盖，可安全删除；当前日志在 `web/logs/`。

## 操作文档

- **接入新服务**：优先用项目技能 `add-site-to-homepage`（`.claude/skills/`）；详细步骤见 [doc/add-site.md](doc/add-site.md)（含代理模式选择、curl 命令、权限分配、故障排查）

## 验证方式

无测试套件、无 lint。改动后用 `--debug` 模式跑起来，curl 验证路由/API，观察 `journalctl -u homepage -f` 与 `web/logs/web.log`。
