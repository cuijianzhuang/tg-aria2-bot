FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# 非 root 运行（最小权限原则）。UID/GID 跟 docker-compose.yml 里 aria2 容器的
# PUID/PGID=1000 保持一致，方便下载目录跨容器权限对齐。
#
# 全新安装由 install.sh 自动 chown 好 bind mount 目录/文件；从旧版本（root
# 运行）升级的部署需要手动执行一次（详见 README「从旧版本升级」）：
#   sudo chown -R 1000:1000 downloads data aria2-config .env
# 这些都是 bind mount，镜像里的 chown 只对镜像自身文件生效，管不到宿主机上
# 已存在的目录/文件。
RUN groupadd -g 1000 app && useradd -u 1000 -g app -m -d /home/app app \
    && chown -R app:app /app
USER app

CMD ["python", "-m", "bot.main"]
