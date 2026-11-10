# 固定镜像部署

适合希望服务器只安装 Docker、避免 Python/依赖/编译环境差异的部署。
支持 Linux amd64、arm64。运行仍需要 Telegram 网络连通性、可用磁盘和正确凭据。

## 发布镜像（维护者）

推送 `v*` 标签，或手动运行 `Release container images` 工作流。测试通过后构建
两种架构并发布到 GHCR：

- `ghcr.io/<owner>/<repo>/bot:<tag>`
- `ghcr.io/<owner>/<repo>/ariang:<tag>`
- 同时发布 `sha-<完整提交号>`，不生成浮动的 latest 标签。

首次发布后，确认两个 GHCR package 对部署用户可读。私有 package 先在服务器
执行 `docker login ghcr.io`，凭据留在 Docker 的凭据存储中，不要写进项目配置。
本地修改不会自动发布镜像；只有工作流成功后相应版本才可拉取。

Python/nginx 基础镜像、aria2/Bot API 镜像使用固定摘要；AriaNg 使用固定版本；
Python 运行依赖（含间接依赖）记录在 `requirements.lock`。
升级依赖时需要同步更新锁文件/摘要、构建并验证，固定版本不代表永久无需更新。

## 首次安装

服务器安装 Docker Engine、Compose v2 插件、Git 和 Bash，不需要 Python、pip、gcc。
克隆仓库并进入目录：

```bash
cp .env.example .env
# 编辑 .env：BOT_TOKEN、API_ID（整数）、API_HASH、ARIA2_SECRET、ALLOWED_USER_IDS
# 使用默认容器路径：DB_PATH=data/tasks.db、DOWNLOAD_DIR=/downloads
# 需要管理后台时设置 COMPOSE_PROFILES=web，并配置 ADMIN_PASSWORD。
# 公网使用后台时配置 HTTPS 反向代理；可设 WEB_BIND=127.0.0.1。

# 把 vX.Y.Z 换成已成功发布的版本号。
bash scripts/deploy-images.sh vX.Y.Z
```

脚本拉取应用镜像，解析成内容摘要，再拉取固定版本的依赖服务；通过镜像内的
Python 统一数据目录、配置文件的 UID/GID 为 1000，不修改整个下载目录的已有文件。
新安装时 aria2 容器初始化 `/config`，已有 `aria2-config` 会继续使用。
启动自检检查配置字段、目录权限、aria2 RPC 鉴权和 Bot API getMe；服务未就绪时会重试。
自检不会把密钥、token、远端响应体写进错误提示。

默认的 `.env.example` 含演示凭据，必须替换。不要在对话或公开日志中粘贴真实凭据。

实际镜像写入 `.images.env`，上次的应用镜像摘要保存在 `.images.env.previous`。
两个文件均已加入 gitignore。日常查看状态和日志使用同一套参数：

```bash
docker compose --env-file .env --env-file .images.env -f docker-compose.images.yml ps
docker compose --env-file .env --env-file .images.env -f docker-compose.images.yml logs --tail 100 bot
```

## 升级与回退

升级：运行相同脚本并指定新版本。应用切换前先做依赖检查；失败会返回非零状态。
应用切换后的启动失败需要检查日志并手动回退，脚本不宣称自动恢复全部服务。

```bash
bash scripts/deploy-images.sh v新版本

# 回退到上次应用镜像；不会删除配置、数据库或下载文件。
cp .images.env.previous .images.env
docker compose --env-file .env --env-file .images.env -f docker-compose.images.yml up -d --no-build
```

镜像回退不会撤销数据库迁移。重大升级前另外备份 `.env`、SQLite（用在线 backup
接口，不直接复制运行中的主数据库文件）、`data/metalinks`、`data/torrents`、
aria2 配置和会话；必要时一并备份 Telegram Bot API 数据卷。

此入口与原来的 `tg-aria2.sh` 源码部署并存，不能交替使用同一套容器。
仓库已有 master 推送自动部署；选择镜像部署后，在仓库 Actions Variables 中设置
`DEPLOY_MODE=images`，让旧的 SSH 源码部署任务跳过。镜像发布不自动修改服务器。
Fork 仓库通过 `IMAGE_REPOSITORY=ghcr.io/<owner>/<repo>` 指定自己的镜像前缀。
