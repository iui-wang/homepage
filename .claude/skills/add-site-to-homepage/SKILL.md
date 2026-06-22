---
name: add-site-to-homepage
description: 把新服务接入 homepage 反向代理门户（添加路由、分配权限、验证）
---

读取 /home/wyw/homepage/doc/add-site.md，按照其中的步骤帮用户把新服务接入 homepage。

如果用户没有提供以下信息，先询问：
- 服务名称（key，用于 URL 路径）
- 上游端口
- 是否需要剥前缀（strip_prefix）—— 不确定时建议先试 true
- 需要分配权限的用户（admin 不需要）
