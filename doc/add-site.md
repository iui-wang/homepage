# 如何把新服务接入 homepage

## 背景

homepage 是运行在 `/home/wyw/homepage` 的 Flask 反向代理门户，对外唯一入口是 `:80`。
所有上游服务必须通过 homepage 的路由系统暴露，直接对公网监听会绕过登录保护。

接入一个新服务需要三步：
1. 确认上游服务已在本机运行并监听（必须绑 `127.0.0.1`，不对公网）
2. 通过 API 添加路由
3. 给需要的用户分配该路由权限（admin 天然拥有全部路由，无需分配）

---

## 第一步：确认上游服务

```bash
# 确认服务在 127.0.0.1 上监听（非 0.0.0.0）
ss -tlnp | grep <端口>
# 期望看到: 127.0.0.1:<端口>
```

若服务监听在 `0.0.0.0`，需修改服务配置改为 `127.0.0.1`，再重启。

---

## 第二步：选择代理模式与机器

URL 是机器+服务两级结构：`/<machine>/<key>/...`。`machine` 是机器 slug，固定四台：
`tokyo`（东京腾讯云）、`shanghai`（上海阿里云）、`m720q`（联想 M720Q）、`shin`（东京シンVPS）。
添加路由时必须指定 `machine` 字段；同一 key 可以在多台机器上各有一条路由。

| 模式 | strip_prefix | 何时使用 |
|---|---|---|
| **普通模式** | `false`（默认） | 上游服务知道自己跑在 `/<machine>/<key>` 路径下（如 ttyd 用 `--base-path /tokyo/tyyd` 启动） |
| **剥前缀模式** | `true` | 上游服务跑在根路径 `/`，不需要任何路径配置（如 code-server、大多数现成服务） |

**判断方法**：不确定时，先试剥前缀模式（`strip_prefix: true`）。大多数现成服务（Grafana、Jupyter、各种 dashboard）都跑在根路径。

两种模式的区别（以 machine=tokyo、key=myapp 为例）：
- 普通模式：浏览器访问 `/tokyo/myapp/page` → 代理**完整原路径** `/tokyo/myapp/page` 转发到上游
- 剥前缀模式：浏览器访问 `/tokyo/myapp/page` → 代理转发 `/page` 到上游（剥掉两段前缀）

---

## 第三步：添加路由

### 路由 key 规范

- 只能用字母、数字、`_`、`-`，长度 1~40
- 不能用保留字：`api`、`static`、`login`、`logout`、`favicon.ico`、`health`
- 建议用英文小写短词，如 `grafana`、`jupyter`、`code`

### 登录获取 session

```bash
curl -s -c /tmp/hp_cookie.txt -X POST http://127.0.0.1/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<admin密码>"}' | python3 -m json.tool
# 期望返回: {"ok": true}
```

### 添加路由（普通模式示例）

```bash
curl -s -b /tmp/hp_cookie.txt -X POST http://127.0.0.1/api/routes \
  -H "Content-Type: application/json" \
  -d '{
    "machine": "tokyo",
    "key": "myapp",
    "display_name": "我的应用",
    "upstream_host": "127.0.0.1",
    "upstream_port": 8080,
    "strip_prefix": false
  }' | python3 -m json.tool
# 期望返回: {"ok": true}
```

### 添加路由（剥前缀模式示例）

```bash
curl -s -b /tmp/hp_cookie.txt -X POST http://127.0.0.1/api/routes \
  -H "Content-Type: application/json" \
  -d '{
    "machine": "tokyo",
    "key": "myapp",
    "display_name": "我的应用",
    "upstream_host": "127.0.0.1",
    "upstream_port": 8080,
    "strip_prefix": true
  }' | python3 -m json.tool
# 期望返回: {"ok": true}
```

> `machine` 必填，且必须是 machines 表里的 slug（tokyo / shanghai / m720q / shin），否则返回 400。

---

## 第四步：分配用户权限

admin 账号天然访问全部路由，**只有非 admin 用户需要手动分配**。

### 查询用户 ID

```bash
curl -s -b /tmp/hp_cookie.txt http://127.0.0.1/api/users | python3 -m json.tool
# 在 "users" 数组里找到目标用户的 "id"
```

### 分配路由（覆盖写，需列出该用户所有路由 key）

```bash
# 假设用户 id=2，将 myapp、bill 两个路由都给他
curl -s -b /tmp/hp_cookie.txt \
  -X POST http://127.0.0.1/api/users/2/routes \
  -H "Content-Type: application/json" \
  -d '{"routes": ["myapp", "bill"]}' | python3 -m json.tool
# 期望返回: {"ok": true}
```

> ⚠️ `routes` 字段是**覆盖写**：传入列表会替换该用户之前全部路由权限。
> 分配新路由前先从 `/api/users` 查出该用户当前已有的路由，合并后再提交。

---

## 第五步：验证

```bash
# 测试路由是否正常响应（用 admin session；URL 为 /<machine>/<key>/）
curl -s -b /tmp/hp_cookie.txt -o /dev/null -w "%{http_code}" http://127.0.0.1/tokyo/myapp/
# 期望: 200 或 302（302 是上游的重定向，正常）
# 如果返回 404 → machine 或 key 写错，或路由未添加成功
# 如果返回 502 → 上游服务未启动或端口写错
# 如果返回 403 → 当前用户没有该路由权限
```

---

## 常见问题

**Q: 返回 502 Bad Gateway**
- 确认上游端口正确：`ss -tlnp | grep <端口>`
- 确认上游服务正在运行：`systemctl status <service>`
- 确认上游绑的是 `127.0.0.1` 而非某个外网 IP

**Q: 页面能打开但样式/JS 全失效**
- 症状：上游服务用了绝对路径静态资源（如 `/static/...`），在剥前缀模式下被路由到了别处
- 解法：改用普通模式，同时在上游服务配置 base-path 为 `/<machine>/<key>`；或检查上游是否有配置 `base-path` / `root-path` 的选项

**Q: WebSocket 不通（如终端黑屏）**
- homepage 已内置 WebSocket 代理，不需要额外配置
- 检查上游是否监听在正确地址和端口

**Q: 修改了路由配置（如改端口）后不生效**
- 路由改动是实时生效的，不需要重启 homepage
- 可用 `PUT /api/routes/<id>` 接口更新（先从 `GET /api/routes` 取路由 id），或在 homepage 管理界面操作

**Q: 想删除路由**
```bash
# 先查路由 id
curl -s -b /tmp/hp_cookie.txt http://127.0.0.1/api/routes | python3 -m json.tool
# 再删除
curl -s -b /tmp/hp_cookie.txt -X DELETE http://127.0.0.1/api/routes/<id>
```
