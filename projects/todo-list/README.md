# Todo List

最简单的待办清单应用 — 单页 HTML，Docker 部署。

## 功能

- 添加待办事项（输入框 + 回车）
- 标记完成（点击文字切换划线状态）
- 删除待办
- 数据持久化（localStorage）

## 技术栈

- 纯 HTML/CSS/JS，无依赖
- Docker：nginx:alpine 托管静态文件
- 通过 nginx-proxy 反向代理 + ACME 自动 HTTPS

## 部署

```bash
cd projects/todo-list
docker compose up -d --build
```

部署后通过 nginx-proxy 自动反代，访问：**https://todo.${S_DOMAIN}**（如 `https://todo.ai.jingtao.fun`）

### 环境变量（docker-compose.yml）

域名通过 `S_DOMAIN` 环境变量配置（需在系统环境中设置，如 `S_DOMAIN=ai.jingtao.fun`）。

| 变量 | 值 | 说明 |
|------|-----|------|
| `VIRTUAL_HOST` | `todo.${S_DOMAIN}` | nginx-proxy 反代域名 |
| `VIRTUAL_PORT` | `80` | 容器内服务端口 |
| `LETSENCRYPT_HOST` | `todo.${S_DOMAIN}` | ACME 自动签发 HTTPS 证书 |

## 文件结构

```
todo-list/
├── README.md              # 本文件
├── index.html             # 应用页面（HTML + CSS + JS）
├── Dockerfile             # nginx:alpine + 静态文件
└── docker-compose.yml     # Docker Compose 部署配置
```

## 本地预览

直接用浏览器打开 `index.html` 即可，无需服务器。
