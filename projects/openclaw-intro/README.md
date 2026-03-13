# OpenClaw Intro

OpenClaw 介绍页 —— 一个单页展示站点，介绍 OpenClaw 开源 AI 智能体网关的架构、核心模块与工作原理。

## 技术栈

- 纯静态 HTML / CSS / JavaScript（无框架依赖）
- Nginx Alpine 容器托管
- 通过 nginx-proxy + Let's Encrypt 自动 HTTPS

## 本地开发

直接在浏览器中打开 `index.html` 即可预览。

## Docker 部署

确保服务器上已配置 `S_DOMAIN` 和 `S_EMAIL` 环境变量，然后执行：

```bash
cd projects/openclaw-intro
docker compose up -d
```

站点将部署到 `https://oc-intro.<S_DOMAIN>`。

停止服务：

```bash
docker compose down
```

## 目录结构

```
openclaw-intro/
├── index.html          # 主页面
├── css/style.css       # 样式（暗色主题、玻璃拟态、动画）
├── js/main.js          # 交互（粒子背景、打字效果、卡片展开）
├── Dockerfile          # Nginx 容器构建
├── docker-compose.yml  # 部署配置
└── README.md
```
