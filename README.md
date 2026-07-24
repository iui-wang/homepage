# homepage

反向代理路由门户。监听 `0.0.0.0:80`，是本机唯一的公网入口；把 `{IP}/<route>/...`
反向代理到各路由配置的上游 `ip:port`（原路径透传）。后端 Flask，前端小清新风格。

## 功能

- **登录门**：用户名 + 密码登录；会话用签名 Cookie（默认 30 天，可在「配置」改）。
- **路由**：展示当前用户有权限的网站卡片，点击当前页跳转（经代理）。默认两站：
  - `xiangyun` → 博弘翔云1号私募证券投资基金A（127.0.0.1:8848）
  - `bill` → 账单（127.0.0.1:5057）
- **用户管理**（仅 admin）：增删用户、按用户勾选可见路由。admin 不可删、恒有全权。
- **活跃记录**（仅 admin）：顶部「每个用户最后请求时间」，下方全量请求明细（SQLite，
  封顶 10 万条，分页每页 10，可按用户名过滤）。仅记录有意义请求（过滤静态资源）。
- **配置**（仅 admin）：改会话时长；增删路由 + 配每个路由上游 `ip:port`（上游可非 127，
  例如公网或 VPN 局域网里的服务，原路径透传）。
- **我的**：改密码。新密码须 **超过 8 位，且含大写、小写、数字、特殊符号**。

## 安全（fail2ban）

登录失败写 `logs/auth.log`（含客户端 IP）。fail2ban jail `homepage-auth`：
**1 小时内累计 5 次失败 → 封该 IP 的 80 端口 24 小时**。前端登录失败会弹框告知此规则。

## 目录

```
config.toml            启动配置：admin 初始密码(明文)、secret_key、监听、路径
web/app.py             Flask：登录、反向代理、各 API
web/db.py              SQLite schema 与读写
web/logger.py          统一日志
web/templates/         登录页、SPA、错误页
web/static/            style.css、app.js
deploy/homepage.service        systemd 单元
deploy/homepage-auth.conf      fail2ban filter
deploy/homepage-jail.local     fail2ban jail
data/homepage.db       SQLite（首次启动自动建库种数据）
logs/auth.log          登录失败日志（fail2ban 读）
```

## 部署

```bash
# 1. 依赖（conda env: homepage）
/root/miniconda3/envs/homepage/bin/pip install flask requests

# 2. systemd
cp deploy/homepage.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now homepage

# 3. fail2ban
cp deploy/homepage-auth.conf /etc/fail2ban/filter.d/
cp deploy/homepage-jail.local /etc/fail2ban/jail.d/homepage.local
systemctl restart fail2ban
```

admin 初始密码见 `config.toml` 的 `admin_initial_password`，首次登录后请在「我的」改密。

## 上游注意

代理为 **原路径透传**：`{IP}/bill/x` 会原样转发为 `上游/bill/x`。因此新增路由时，
上游服务需在相同前缀下提供服务（本地三站即如此）。上游若返回指向自身内网地址的
重定向，homepage 会把 `Location` 改写为相对路径，避免内网地址泄露给浏览器。
