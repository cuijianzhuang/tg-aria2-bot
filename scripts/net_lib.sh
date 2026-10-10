# shellcheck shell=bash
# 端口探测小工具，install_bare.sh / install_docker.sh / manage.sh 共用（source 进来用）。

# 端口上有没有程序在监听。用 bash 自带的 /dev/tcp 探测，不依赖 ss/netstat
# （精简系统上未必装了）
port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

# 尽量说出是谁占着端口（只用于提示，拿不到就是"未知程序"）
port_owner() {
  local out=""
  if command -v ss >/dev/null 2>&1; then
    out="$(ss -Hltnp "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2 || true)"
  elif command -v lsof >/dev/null 2>&1; then
    out="$(lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1}' || true)"
  elif command -v netstat >/dev/null 2>&1; then
    out="$(netstat -ltnp 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {print $7; exit}' || true)"
  fi
  echo "${out:-未知程序}"
}

# 端口上是不是一个能用的 telegram-bot-api。按协议判断而不是按进程名：
# 拿一个无效 token 调 getMe，Bot API 服务一定回 {"ok":false,"error_code":...}
port_is_botapi() {
  local body
  body="$(curl -s -m 5 "http://127.0.0.1:$1/bot0:invalid/getMe" 2>/dev/null || true)"
  [[ "$body" == *'"ok":false'* && "$body" == *'"error_code"'* ]]
}

# 端口上是不是本项目自带的 Web 管理后台（首页 <title> 固定是
# "tg-aria2-bot 管理后台"，未登录也能拿到）
port_is_our_web() {
  local body
  body="$(curl -s -m 5 "http://127.0.0.1:$1/" 2>/dev/null || true)"
  [[ "$body" == *"tg-aria2-bot 管理后台"* ]]
}

# find_free_port <起始> <结束> [要避开的端口...] —— 输出区间内第一个空闲端口，
# 没有则返回 1
find_free_port() {
  local start="$1" end="$2" p x skip
  shift 2
  for (( p = start; p <= end; p++ )); do
    skip=0
    for x in "$@"; do [[ "$p" == "$x" ]] && skip=1; done
    [[ "$skip" -eq 1 ]] && continue
    port_in_use "$p" || { echo "$p"; return 0; }
  done
  return 1
}
