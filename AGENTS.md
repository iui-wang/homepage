# AGENTS.md

AI 智能体在本仓库工作时的项目说明，与代码保持一致，以此为准（README.md 已过时，别信）。

## 项目简介

反向代理路由门户：登录门 + 用户/路由管理 + 活跃记录 + 机器监控。Flask 后端 + 原生 JS SPA，SQLite 存储。它是所有上游服务（只监听内网）的唯一公网入口。代码在本机（东京腾讯云）维护，生产部署在上海阿里云：域名 ui-chan.cn，Caddy HTTPS :443 反代到本服务。

## 启动与运行

```bash
# 直接运行（conda 环境 homepage；追加 --debug 开调试模式）
/home/wyw/miniconda3/envs/homepage/bin/python web/app.py --config config.toml

# systemd
systemctl restart homepage && journalctl -u homepage -f

# 日志
tail -f /var/log/homepage-web.log  # systemd 重定向的 stdout/stderr（见 deploy/homepage.service）
tail -f web/logs/web.log           # logger.py 结构化日志（每日午夜滚动，留 14 天）
tail -f logs/auth.log              # 登录失败记录，fail2ban 读它
```

依赖（用同一 conda 环境的 pip）：`pip install flask flask-sock requests websocket-client`

## 架构

```
web/app.py      Flask 入口：登录/鉴权装饰器、管理 API、HTTP/WS 反向代理、监控采样
web/db.py       SQLite schema 与读写（无 ORM，直接 sqlite3）
web/logger.py   单例 logger；写 web/logs/（绝对路径，与 CWD 无关），LOG_DIR 环境变量可覆盖
web/templates/  login.html、index.html（SPA）、forbidden/notfound/upstream_error.html
web/static/     app.js（全部前端逻辑）、style.css、PWA 图标与 site.webmanifest
config.toml     启动配置（secret_key、admin 密码、监听、路径）——已 gitignore
data/homepage.db  SQLite，首次启动自动建库种数据（默认路由 xiangyun、bill）
deploy/         systemd 单元与 fail2ban 配置；也存放接入本门户的其他服务单元（code-server、ttyd 等）
doc/add-site.md 接入新服务的操作文档（代理模式选择、curl 示例、权限分配、排障）
```

### 关键设计点

- **路由分发**：URL 两级结构 `/<machine>/<key>/<rest>`，catch-all 取前两段按 (machine, key) 查 routes 表（唯一约束同），查不到 404。机器 slug 固定在 machines 表（tokyo / shanghai / m720q / shin）。路由改动实时生效，无需重启。
- **strip_prefix**：普通路由原路径透传（上游应用挂在 `/<machine>/<key>` 前缀下）；`strip_prefix=1` 剥掉两段前缀再转发，用于 code-server 等根路径服务。选模式见 doc/add-site.md。
- **WebSocket 反代**：flask-sock 接浏览器（子协议固定协商 `tty`，25 秒 ping 保活），websocket-client 连上游（透传客户端子协议），双向 pump 保留 text/binary 帧型。与 HTTP 同前缀共存，Werkzeug 按 Upgrade 头分流。
- **身份头注入（auth proxy）**：转发前剥掉客户端带来的 `X-Forwarded-User` / `X-Forwarded-User-Id` / `X-Forwarded-Admin`（防伪造），再注入当前登录用户的真实值。上游只监听内网、唯一入口是本门户，可直接信任这三个头，无需自建登录。
- **真实客户端 IP**：只对回环对端（本机 Caddy）信任 `X-Forwarded-For` 首段，其余用对端 IP——盲目信任 XFF 会让攻击者伪造 IP 绕过 fail2ban。
- **流式转发**：`stream=True` 逐块（64KB），连接超时 5 秒、单次读超时 300 秒（慢下载不被总时长掐断），保留 Range 的 206/Content-Range 语义。gzip 响应（requests 已解压）剔除 `Content-Length`，未压缩的透传（下载进度条依赖它）。上游返回指向自身内网地址的 `Location` 改写成相对路径，避免泄露内网地址。
- **活跃记录**：过滤静态资源和 `/api/`、`/static/` 前缀，只存路径前两段。合并键 username+path+ip+method 四元组完全相同，且距最新一条的 `start_ts` ≤60 秒则并入（累加 count、推进 end_ts），否则新开——单条最长只覆盖 60 秒。封顶 10 万条（超出删最旧）。
- **卡片点击排序**：首页按近 72 小时点击数降序，相同按 `routes.sort_order`（list.sort 稳定排序）。点击只统计前端点卡片动作（`POST /api/track-click`），刻意不复用 active_log。记录存纯内存（`_CLICKS`），不落库，重启清零——主人明确认可。
- **会话时长**：存 settings 表，`/api/settings` 改后立即生效（无需重启），取值 1~3650 天。
- **机器监控**：守护线程每 10 秒直接读 `/proc/stat`、`/proc/meminfo`、`statvfs('/')`（刻意不依赖 psutil）写 metrics 表，每小时清一次过期数据（保留 7 天）。debug 模式靠 `WERKZEUG_RUN_MAIN` 判重避免重载子进程双采。`/api/metrics?window=...`（1m~7d，见 `METRICS_WINDOWS`）按桶取均值降采样到 ≤400 点，仅 admin 可见。
- **admin 密码**：以 config.toml 的 `admin_initial_password` 为唯一来源，每次启动 `init_db` 同步覆盖到库；admin 不能通过 UI/API 改密（返回 403），改 config.toml 后重启生效。
- **管理约束**：用户名限 `[A-Za-z0-9_.-]{2,32}`；非 admin 密码须超 8 位且含大小写、数字、特殊符号（用户改密与 admin 创建用户同一规则）；禁止创建/删除 admin、禁止给 admin 分配路由；编辑路由（PUT）不可改 key。

### SQLite 表

users / machines（slug 主键）/ routes（UNIQUE(machine, key)）/ user_routes / settings / active_log / metrics。授权按服务 key：授予某 key 即授予它在所有机器上的路由行；admin 恒有全部路由，不走 user_routes 表。旧库迁移幂等：routes 缺 machine 列时重建并按 upstream_host 回填（映射见 db.py `HOST_TO_MACHINE`）；active_log 检测到旧 `ts` 单列结构会 drop 重建并清空历史。

## fail2ban

```bash
deploy/homepage-auth.conf  → /etc/fail2ban/filter.d/
deploy/homepage-jail.local → /etc/fail2ban/jail.d/homepage.local
systemctl restart fail2ban
```

规则：1 小时内登录失败 5 次 → 封该 IP 的 443 端口（TCP+UDP，iptables-multiport）24 小时。

## 注意事项

- `config.toml`、`data/`、`logs/` 已 gitignore。`config.toml.example` 里的 `/root/...` 路径是旧的，实际部署在 `/home/wyw/homepage`，以 web/app.py 的 `DEFAULT_CONFIG` 和 deploy/homepage.service 为准。
- 新增路由 key 不能撞 `RESERVED_ROUTE_KEYS`（web/db.py）。上游服务必须绑 `127.0.0.1`，直接对公网监听会绕过登录门。
- `routes_repo/` 是未接入的半成品：app.py 末尾注释提到的 `setup_routes_repo()` 在代码中不存在，目录内容没有任何加载器引用。别去找不存在的实现；`sys.modules["app"]` 别名保留即可（无副作用）。
- `web/` 下的 `web.log` 和 `web.log.2026-06-*` 是 logger 改绝对路径前的遗留文件，可安全删除；当前日志在 `web/logs/`。
- deploy/homepage.service 以 `User=root` 运行，工作目录 `web/`，stdout/stderr 追加到 `/var/log/homepage-web.log`。

## 操作文档

接入新服务：优先用项目技能 `add-site-to-homepage`（`.claude/skills/`）；详细步骤见 [doc/add-site.md](doc/add-site.md)。

## 验证方式

无测试套件、无 lint。改动后用 `--debug` 模式跑起来，curl 验证路由/API，观察 `journalctl -u homepage -f` 与 `web/logs/web.log`。
