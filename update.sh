#!/usr/bin/env bash
#
# tg-aria2-bot 一键升级：拉取最新代码 → 备份 → 应用 → 健康检查 → 失败自动回滚
#
# 用法（在仓库目录里，root 权限）：
#   sudo ./update.sh                 # 交互式：列出新提交，确认后升级
#   sudo ./update.sh --check         # 只看有没有新版本、有哪些提交，不做任何改动
#   sudo ./update.sh -y              # 不询问，直接升级（适合 cron / CI）
#
# 选项：
#   -y, --yes          不询问确认
#   --check            只检查更新，不升级
#   --branch NAME      跟踪的远端分支（默认当前分支）
#   --reset            用 git reset --hard 对齐远端，而不是 fast-forward
#                      （本地对 git 跟踪文件有改动时也会被丢弃，aria2-config/ 除外）
#   --force            没有新提交也照样重新应用一遍（重装依赖/重建镜像/重启）
#   --pull-images      docker 模式：顺带拉取 telegram-bot-api / aria2 / 基础镜像的新版本
#   --no-rollback      健康检查失败时不自动回滚（留着现场排查）
#   --dir PATH         仓库目录（默认脚本所在目录；CI 从临时文件运行时用）
#
# 部署模式（docker / bare）从 .env 自动识别，跟 install.sh 用同一个判据。
#
# 两阶段执行：本脚本先完成 拉代码+备份，然后 exec 新版本的 update.sh 去做
# 应用/健康检查/回滚——这样新版本新增的升级步骤（新依赖、新的 systemd 单元、
# 新的 .env 键）第一次升级就能生效，不用跑两遍。
#
# 升级不会碰：.env 里已有的值、data/（数据库）、downloads/、
# aria2-config/ 里你改过的配置（会先备份再原样恢复）。

set -euo pipefail

log()  { printf '\033[1;32m[update]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$1" >&2; exit 1; }

ASSUME_YES=0
CHECK_ONLY=0
BRANCH=""
RESET=0
FORCE=0
PULL_IMAGES=0
ROLLBACK=1
REPO_DIR=""
# 第二阶段（内部用）：--_apply <旧提交> <备份目录>
APPLY_FROM=""
BACKUP_DIR=""
# 回滚后用旧代码重新应用时置 1，避免回滚失败再回滚的死循环
IS_ROLLBACK=0
# 第一阶段确实更新了代码、需要进入第二阶段时置 1
UPDATE_READY=0

BACKUP_KEEP=5
HEALTH_WAIT_SECONDS="${UPDATE_HEALTH_WAIT:-20}"

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes) ASSUME_YES=1; shift ;;
      --check) CHECK_ONLY=1; shift ;;
      --branch) BRANCH="$2"; shift 2 ;;
      --reset) RESET=1; shift ;;
      --force) FORCE=1; shift ;;
      --pull-images) PULL_IMAGES=1; shift ;;
      --no-rollback) ROLLBACK=0; shift ;;
      --dir) REPO_DIR="$2"; shift 2 ;;
      --_apply) APPLY_FROM="$2"; BACKUP_DIR="$3"; shift 3 ;;
      --_rollback) IS_ROLLBACK=1; shift ;;
      -h|--help) sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "未知参数: $1（--help 查看用法）" ;;
    esac
  done
}

# ---------------------------------------------------------------- 公共工具

detect_mode() {
  local url
  url="$(env_get BOT_API_URL)"
  case "$url" in
    http://telegram-bot-api:8081) echo docker ;;
    http://127.0.0.1:8081) echo bare ;;
    *) echo "" ;;
  esac
}

compose() { docker compose "$@"; }

# 当前 bare 部署注册了哪些我们自己的 systemd 服务（web 是可选的）
bare_units() {
  local u
  for u in tg-aria2-bot tg-aria2-web; do
    [[ -f "/etc/systemd/system/${u}.service" ]] && echo "$u"
  done
  return 0
}

backup_db() {
  local src="$1" dst="$2"
  [[ -f "$src" ]] || return 0
  # bot/web 运行中、WAL 模式下直接 cp 主库文件可能拿到不一致的快照；
  # 优先用 SQLite 在线备份 API，宿主机没有 python3/sqlite3 时才退回 cp
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$src" "$dst" && return 0
  fi
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".backup '$dst'" && return 0
  fi
  warn "宿主机没有 python3/sqlite3，数据库按文件直接复制（运行中可能不完全一致）"
  cp -p "$src" "$dst"
  [[ -f "${src}-wal" ]] && cp -p "${src}-wal" "${dst}-wal"
  return 0
}

host_db_path() {
  if [[ "$MODE" == "docker" ]]; then
    echo "$REPO_DIR/data/tasks.db"   # 容器内 /app/data 映射到仓库的 ./data
  else
    local p
    p="$(env_get DB_PATH)"
    echo "${p:-$REPO_DIR/data/tasks.db}"
  fi
}

prune_backups() {
  local dirs
  mapfile -t dirs < <(ls -1d "$REPO_DIR"/backups/*/ 2>/dev/null | sort)
  local n=${#dirs[@]}
  if (( n > BACKUP_KEEP )); then
    local i
    for (( i = 0; i < n - BACKUP_KEEP; i++ )); do rm -rf "${dirs[$i]}"; done
  fi
}

# ---------------------------------------------------------------- 第一阶段：拉代码

phase_fetch() {
  [[ -d .git ]] || die "$REPO_DIR 不是 git 仓库。升级脚本依赖 git clone 方式的部署（README「一键安装」）。"

  BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
  [[ "$BRANCH" != "HEAD" ]] || die "当前处于 detached HEAD，请用 --branch 指定要跟踪的分支"

  log "检查更新（origin/${BRANCH}）..."
  git fetch --quiet origin "$BRANCH" || die "git fetch 失败，检查网络或远端地址：$(git remote get-url origin)"

  local old new
  old="$(git rev-parse HEAD)"
  new="$(git rev-parse "origin/${BRANCH}")"

  if [[ "$old" == "$new" ]]; then
    log "已经是最新版本（$(git log -1 --format='%h %s' HEAD)）"
    if [[ "$FORCE" -eq 0 || "$CHECK_ONLY" -eq 1 ]]; then
      return 0
    fi
    log "--force：没有新提交，照样重新应用一遍"
  else
    if ! git merge-base --is-ancestor "$old" "$new"; then
      if [[ "$RESET" -eq 0 ]]; then
        die "本地分支和 origin/${BRANCH} 已经分叉（本地有远端没有的提交），无法快进。
确认本地提交可以丢弃的话加 --reset 强制对齐远端。"
      fi
      warn "本地分支和远端分叉，--reset 将丢弃本地提交"
    fi
    echo
    log "发现 $(git rev-list --count "${old}..${new}") 个新提交："
    git --no-pager log --format='  %C(yellow)%h%Creset %s %C(dim)(%cr)%Creset' "${old}..${new}" | head -30
    echo
    if git diff --quiet "$old" "$new" -- requirements.txt; then :; else log "· 依赖有变化（requirements.txt），会自动重新安装"; fi
    if git diff --quiet "$old" "$new" -- .env.example; then :; else log "· .env.example 有新配置项，按需对照添加（git diff ${old:0:7} ${new:0:7} -- .env.example）"; fi
    if git diff --quiet "$old" "$new" -- aria2-config; then :; else warn "· aria2-config/ 模板有变化——你本地的配置会保留不被覆盖，需要的话手动对照：git diff ${old:0:7} ${new:0:7} -- aria2-config"; fi
  fi

  if [[ "$CHECK_ONLY" -eq 1 ]]; then
    log "（--check 模式，未做任何改动；升级请运行 sudo ./update.sh）"
    return 0
  fi

  if [[ "$ASSUME_YES" -eq 0 ]]; then
    local ans
    read -rp "现在升级？[y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { log "已取消"; return 0; }
  fi

  # ---- 备份 ----
  BACKUP_DIR="$REPO_DIR/backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$REPO_DIR/backups" "$BACKUP_DIR"
  [[ -f .env ]] && cp -p .env "$BACKUP_DIR/.env"
  backup_db "$(host_db_path)" "$BACKUP_DIR/tasks.db"
  echo "$old" > "$BACKUP_DIR/COMMIT"
  log "已备份 .env / 数据库 / 当前版本号到 ${BACKUP_DIR#"$REPO_DIR"/}"

  # ---- aria2-config/：git 跟踪的模板，但运行时会被改写 ----
  # （install.sh 写入 rpc-secret、aria2-pro 容器启动时改写、设置菜单切换
  # rclone 钩子……）这些本地改动会让 git pull 直接拒绝。先存一份，让 git
  # 干净地更新模板，再把用户的版本原样放回去——用户配置优先，上游模板的
  # 改动只提示不自动合并（自动合并配置文件出错的代价远大于手动对照一下）。
  local dirty_conf
  mapfile -t dirty_conf < <(git diff --name-only HEAD -- aria2-config)
  if (( ${#dirty_conf[@]} > 0 )); then
    local f
    for f in "${dirty_conf[@]}"; do
      mkdir -p "$BACKUP_DIR/conf/$(dirname "$f")"
      cp -p "$f" "$BACKUP_DIR/conf/$f"
    done
    git checkout --quiet -- aria2-config
  fi

  # ---- 更新代码 ----
  if [[ "$RESET" -eq 1 ]]; then
    git reset --quiet --hard "origin/${BRANCH}"
  else
    if ! git diff --quiet HEAD; then
      restore_conf
      die "仓库里有未提交的本地改动（aria2-config/ 之外），为避免覆盖已中止：
$(git diff --name-only HEAD | sed 's/^/    /')
处理方式：git stash 暂存，或确认不要后加 --reset 重新运行。"
    fi
    git merge --quiet --ff-only "origin/${BRANCH}"
  fi
  restore_conf
  log "代码已更新到 $(git log -1 --format='%h %s' HEAD)"

  APPLY_FROM="$old"
  UPDATE_READY=1
}

restore_conf() {
  [[ -d "$BACKUP_DIR/conf" ]] || return 0
  (cd "$BACKUP_DIR/conf" && find . -type f -print0) | while IFS= read -r -d '' f; do
    # 原地写回保留属主/inode（aria2 容器以 1000 用户读写这些文件）
    cat "$BACKUP_DIR/conf/$f" > "$REPO_DIR/$f"
  done
}

# ---------------------------------------------------------------- 第二阶段：应用

migrate_env() {
  # 老部署补上新版本依赖的 .env 键（只补不改）
  if [[ "$MODE" == "docker" ]]; then
    if ! env_has COMPOSE_PROFILES; then
      # 以前靠每次手动传 --profile web 决定是否启动 web/ariang；写进 .env 后
      # 手敲 docker compose up/restart 也不会再漏掉它们
      if [[ -n "$(compose --profile web ps -a -q web 2>/dev/null)" ]]; then
        env_set COMPOSE_PROFILES web
      else
        env_set COMPOSE_PROFILES ""
      fi
      log ".env 补充 COMPOSE_PROFILES=$(env_get COMPOSE_PROFILES)"
    fi
  fi
}

apply_docker() {
  command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"
  # 从 root 运行容器的旧版本升级上来时，bind mount 的属主还是 root，
  # 非 root 的 bot/web 容器会 Permission denied——每次升级顺手对齐，幂等
  local dl
  dl="$(env_get HOST_DOWNLOAD_DIR)"
  chown -R 1000:1000 data .env aria2-config 2>/dev/null || true
  chown 1000:1000 "${dl:-./downloads}" 2>/dev/null || true

  if [[ "$PULL_IMAGES" -eq 1 ]]; then
    log "拉取第三方镜像新版本（telegram-bot-api / aria2）"
    compose pull --ignore-buildable --quiet || warn "部分镜像拉取失败，继续使用本地已有版本"
  fi
  log "重建并启动容器（docker compose up -d --build）"
  if [[ "$PULL_IMAGES" -eq 1 ]]; then
    compose build --pull --quiet || return 1
  else
    compose build --quiet || return 1
  fi
  compose up -d --remove-orphans || return 1
}

render_unit() {
  # 跟 install_bare.sh 同一套占位符替换
  local tpl="$1" web_port
  web_port="$(env_get WEB_PORT)"
  sed "s#{{WORKDIR}}#${REPO_DIR}#g; s#{{WEB_PORT}}#${web_port:-8080}#g" "$tpl"
}

apply_bare() {
  [[ -x .venv/bin/python ]] || die "找不到 .venv/bin/python，这不像是 install.sh --mode bare 装出来的部署"

  if [[ -z "$APPLY_FROM" ]] || ! git diff --quiet "$APPLY_FROM" HEAD -- requirements.txt || [[ "$FORCE" -eq 1 ]]; then
    log "安装 Python 依赖"
    .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt || return 1
  else
    log "requirements.txt 无变化，跳过依赖安装"
  fi

  # 先在不动服务的情况下校验新代码能不能正常 import，过不了直接回滚，
  # 服务一秒都不会中断
  log "校验新代码（编译 + 导入）"
  .venv/bin/python -m compileall -q bot || return 1
  .venv/bin/python -c 'import bot.main, bot.web.app' || return 1

  # 只在这次升级里 systemd/ 模板确实变了时才同步单元文件——不能拿模板
  # 去比对已安装的文件，那样会把用户在服务器上手改过的单元（加环境变量、
  # 改 User= 之类）覆盖掉。覆盖前先存一份，回滚时原样放回。
  local u changed=0
  if [[ -n "$APPLY_FROM" ]] && ! git diff --quiet "$APPLY_FROM" HEAD -- systemd/; then
    for u in $(bare_units); do
      if ! render_unit "systemd/${u}.service" | cmp -s - "/etc/systemd/system/${u}.service"; then
        if [[ -n "$BACKUP_DIR" ]]; then
          mkdir -p "$BACKUP_DIR/units"
          cp -p "/etc/systemd/system/${u}.service" "$BACKUP_DIR/units/"
        fi
        render_unit "systemd/${u}.service" > "/etc/systemd/system/${u}.service" || return 1
        log "已更新 /etc/systemd/system/${u}.service（原文件备份在 ${BACKUP_DIR#"$REPO_DIR"/}/units/）"
        changed=1
      fi
    done
  fi
  if [[ "$changed" -eq 1 ]]; then systemctl daemon-reload || return 1; fi

  for u in $(bare_units); do
    log "重启 $u"
    systemctl restart "$u" || return 1
  done
}

health_docker() {
  local svc cid
  local -A before=()
  local services=(bot)
  [[ -n "$(compose ps -q web 2>/dev/null)" ]] && services+=(web)

  for svc in "${services[@]}"; do
    cid="$(compose ps -q "$svc")"
    [[ -n "$cid" ]] || { warn "容器 $svc 没有运行"; return 1; }
    before[$svc]="$(docker inspect -f '{{.RestartCount}}' "$cid")"
  done
  log "健康检查：观察 ${HEALTH_WAIT_SECONDS}s 内容器是否崩溃重启..."
  sleep "$HEALTH_WAIT_SECONDS"
  for svc in "${services[@]}"; do
    cid="$(compose ps -q "$svc")"
    local running restarts
    running="$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null || echo false)"
    restarts="$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 999)"
    if [[ "$running" != "true" || "$restarts" != "${before[$svc]}" ]]; then
      warn "容器 $svc 不健康（running=$running，重启次数 ${before[$svc]} → $restarts），最近日志："
      compose logs --tail 30 "$svc" || true
      return 1
    fi
  done
  return 0
}

health_bare() {
  local u
  local -A before=()
  for u in $(bare_units); do
    before[$u]="$(systemctl show -p NRestarts --value "$u")"
  done
  log "健康检查：观察 ${HEALTH_WAIT_SECONDS}s 内服务是否崩溃重启..."
  sleep "$HEALTH_WAIT_SECONDS"
  for u in $(bare_units); do
    local now
    now="$(systemctl show -p NRestarts --value "$u")"
    if ! systemctl is-active --quiet "$u" || [[ "$now" != "${before[$u]}" ]]; then
      warn "服务 $u 不健康（重启次数 ${before[$u]} → $now），最近日志："
      journalctl -u "$u" -n 30 --no-pager || true
      return 1
    fi
  done
  return 0
}

rollback() {
  local reason="$1"
  if [[ "$IS_ROLLBACK" -eq 1 ]]; then
    die "回滚到旧版本后依然失败（$reason），需要人工处理。备份在 ${BACKUP_DIR}"
  fi
  if [[ "$ROLLBACK" -eq 0 || -z "$APPLY_FROM" ]]; then
    die "升级失败：$reason（未自动回滚，备份在 ${BACKUP_DIR:-无}）"
  fi
  warn "升级失败：$reason —— 自动回滚到 ${APPLY_FROM:0:7}"
  # 回滚只动代码：数据库迁移都是只增不删（加列/加索引），旧代码可以直接用
  # 新 schema；.env 只会被补键，旧代码会忽略不认识的键
  local conf_backup="${BACKUP_DIR:-}/conf"
  [[ -d "$conf_backup" ]] && git checkout --quiet -- aria2-config
  git reset --quiet --hard "$APPLY_FROM"
  [[ -n "${BACKUP_DIR:-}" ]] && restore_conf
  if [[ -d "${BACKUP_DIR:-}/units" ]]; then
    cp -p "$BACKUP_DIR"/units/*.service /etc/systemd/system/
    systemctl daemon-reload
    log "已恢复升级前的 systemd 单元文件"
  fi
  # 用旧版本自己的 update.sh 重新应用一次（旧版本可能还没有本脚本，
  # 那就退回到本脚本里的应用逻辑）
  if [[ -f "$REPO_DIR/update.sh" ]] && grep -q -- '--_rollback' "$REPO_DIR/update.sh"; then
    exec bash "$REPO_DIR/update.sh" --dir "$REPO_DIR" --_apply "" "${BACKUP_DIR:-}" --_rollback --force -y
  fi
  IS_ROLLBACK=1
  APPLY_FROM=""
  FORCE=1
  phase_apply
  exit 1
}

phase_apply() {
  migrate_env
  if [[ "$MODE" == "docker" ]]; then
    apply_docker || rollback "容器构建/启动失败"
    health_docker || rollback "容器启动后崩溃"
  else
    apply_bare || rollback "依赖安装或代码校验失败"
    health_bare || rollback "服务启动后崩溃"
  fi

  if [[ "$IS_ROLLBACK" -eq 1 ]]; then
    warn "已回滚到 $(git log -1 --format='%h %s' HEAD) 并恢复运行。失败原因见上方日志，备份在 ${BACKUP_DIR:-无}"
    exit 1
  fi
  prune_backups
  log "升级完成：$(git log -1 --format='%h %s (%cr)' HEAD)"
  if [[ "$MODE" == "docker" ]]; then
    log "查看日志：docker compose logs -f bot"
  else
    log "查看日志：journalctl -u tg-aria2-bot -f"
  fi
}

# ---------------------------------------------------------------- main

main() {
  parse_args "$@"
  REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
  REPO_DIR="$(cd "$REPO_DIR" && pwd)"
  cd "$REPO_DIR"

  [[ "$EUID" -eq 0 || "$CHECK_ONLY" -eq 1 ]] || die "请用 root 权限运行 (sudo ./update.sh)"
  [[ -f .env ]] || die "$REPO_DIR 下找不到 .env —— 还没安装过（先运行 sudo ./install.sh），或者 --dir 指错了"
  if [[ -f scripts/env_lib.sh ]]; then
    # shellcheck source=scripts/env_lib.sh
    source scripts/env_lib.sh
  else
    # 从还没有 env_lib.sh 的老版本第一次升级（比如 CI 用 origin/master 上的
    # update.sh 去升级服务器上的旧 checkout）：第一阶段只需要读 .env，
    # 第二阶段由新 checkout 里的 update.sh 执行，那时 env_lib.sh 已经在了
    env_get() { grep -m1 "^$1=" "${2:-.env}" 2>/dev/null | cut -d= -f2- || true; }
  fi

  MODE="$(detect_mode)"
  [[ -n "$MODE" ]] || die "无法从 .env 的 BOT_API_URL 识别部署模式（docker/bare）"

  if [[ -z "$APPLY_FROM" && "$IS_ROLLBACK" -eq 0 ]]; then
    log "部署模式：$MODE，仓库：$REPO_DIR"
    phase_fetch
    [[ "$UPDATE_READY" -eq 1 ]] || exit 0
    # 交给（可能是刚拉下来的新版）update.sh 执行第二阶段
    local args=(--dir "$REPO_DIR" --_apply "$APPLY_FROM" "$BACKUP_DIR" -y)
    [[ "$PULL_IMAGES" -eq 1 ]] && args+=(--pull-images)
    [[ "$ROLLBACK" -eq 0 ]] && args+=(--no-rollback)
    [[ "$FORCE" -eq 1 ]] && args+=(--force)
    exec bash "$REPO_DIR/update.sh" "${args[@]}"
  fi

  phase_apply
}

# 整个脚本包在函数里、最后一行才调用：bash 是边读边执行脚本文件的，
# 升级过程中 git 会替换掉 update.sh 本身，先完整解析进内存才安全
main "$@"
