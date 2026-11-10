#!/usr/bin/env bash
# Run from any directory. Never source .env as shell code.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

die() { echo "错误：$*" >&2; exit 1; }
version="${1:-}"
[[ "$version" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]] || die "用法：bash scripts/deploy-images.sh v版本号（也支持 sha-完整提交号）"
command -v docker >/dev/null || die "请先安装 Docker Engine 和 Compose 插件"
docker info >/dev/null 2>&1 || die "无法连接 Docker；检查 Docker 服务和当前用户的访问权限"
docker compose version >/dev/null 2>&1 || die "请安装 Docker Compose v2 插件"
[[ -f .env ]] || die "先复制 .env.example 为 .env，并填写 Telegram 和 aria2 配置"

repository="${IMAGE_REPOSITORY:-ghcr.io/cuijianzhuang/tg-aria2-bot}"
repository="${repository,,}"
bot_image="$repository/bot:$version"
ariang_image="$repository/ariang:$version"

# Pull both application images before replacing the previous release lock.
# Publishing a new package may require making it public in GHCR settings,
# or docker login ghcr.io for private packages.
for image in "$bot_image" "$ariang_image"; do
  docker pull "$image" || die "拉取失败：$image；确认该版本已发布、架构受支持且拥有读取权限"
done
bot_digest="$(docker image inspect "$bot_image" --format '{{index .RepoDigests 0}}')"
ariang_digest="$(docker image inspect "$ariang_image" --format '{{index .RepoDigests 0}}')"
[[ "$bot_digest" == *@sha256:* && "$ariang_digest" == *@sha256:* ]] || die "未能解析镜像摘要"

lock_file="$(mktemp .images.env.XXXXXX)"
trap 'rm -f "$lock_file"' EXIT
printf 'BOT_IMAGE=%s\nARIANG_IMAGE=%s\n' "$bot_digest" "$ariang_digest" > "$lock_file"
compose() { docker compose --env-file .env --env-file "$lock_file" -f docker-compose.images.yml "$@"; }
compose config --quiet
compose pull

mkdir -p data downloads aria2-config
# The image provides Python; the host only needs Docker. Align shared mount
# ownership with the existing bot/web/aria2 UID without recursive chown of downloads.
compose run --rm --no-deps --user 0 --entrypoint python \
  -v "$PWD/aria2-config:/aria2-config" bot -c '
import os
for path in ("/app/data", "/downloads", "/aria2-config", "/app/.env"):
    os.chown(path, 1000, 1000)
for root in ("/app/data", "/aria2-config"):
    for directory, subdirs, files in os.walk(root):
        for name in subdirs + files:
            os.chown(os.path.join(directory, name), 1000, 1000, follow_symlinks=False)
'
compose up -d --no-build aria2 telegram-bot-api
echo "检查配置、目录权限与依赖连接（等待服务就绪）……"
ready=0
for ((attempt=1; attempt<=12; attempt++)); do
  if compose run --rm --no-deps --entrypoint python bot -m bot.preflight; then
    ready=1
    break
  fi
  sleep 5
done
[[ "$ready" -eq 1 ]] || die "启动检查未通过；应用尚未切换版本，请检查配置和依赖日志"
[[ ! -f .images.env ]] || cp .images.env .images.env.previous
cp "$lock_file" .images.env
compose up -d --no-build --remove-orphans --wait --wait-timeout 90
echo "部署完成：$version；实际运行镜像已锁定在 .images.env"
