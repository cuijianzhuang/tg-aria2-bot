# shellcheck shell=bash
# .env 读写小工具，install.sh / update.sh 共用（source 进来用，不单独执行）。
#
# 设计要点：
#   - 只改指定的键，其余行（注释、设置菜单/web 后台写回的运行时配置）原样保留
#     ——以前 install.sh 重跑会整个重写 .env，把 MAX_CONCURRENT/GOFILE_* 这些
#     用户在设置菜单里改过的值全部冲掉
#   - 用 `cat tmp > file` 原地写回而不是 mv：docker 模式下 .env 是单文件 bind
#     mount，rename 覆盖会让容器里看到的还是旧 inode；同时也保留了文件属主
#     （容器内 UID 1000 要能写）和权限
#   - 值按字面处理（awk 从 ENVIRON 取值），不经过 sed 替换串，值里有 # & / 之类
#     的字符也不会出错

# env_get KEY [FILE] —— 输出第一条未注释的 KEY=... 的值；不存在时输出空串
env_get() {
  local key="$1" file="${2:-.env}"
  [[ -f "$file" ]] || return 0
  grep -m1 "^${key}=" "$file" 2>/dev/null | cut -d= -f2- || true
}

# env_has KEY [FILE] —— KEY 是否已经存在（未注释）
env_has() {
  local key="$1" file="${2:-.env}"
  [[ -f "$file" ]] && grep -q "^${key}=" "$file"
}

# env_set KEY VALUE [FILE] —— 存在则就地替换第一处，否则追加到末尾
env_set() {
  local key="$1" value="$2" file="${3:-.env}" tmp
  tmp="$(mktemp)"
  if env_has "$key" "$file"; then
    KEY="$key" VAL="$value" awk '
      BEGIN { k = ENVIRON["KEY"] "="; v = ENVIRON["VAL"] }
      !done && index($0, k) == 1 { print k v; done = 1; next }
      { print }
    ' "$file" > "$tmp"
  else
    if [[ -s "$file" ]]; then
      cat "$file" > "$tmp"
      # 原文件末尾没换行时补一个，避免追加的键粘到上一行后面
      [[ -n "$(tail -c1 "$file")" ]] && echo >> "$tmp"
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

# env_default KEY VALUE [FILE] —— 仅当 KEY 不存在时才写入（不覆盖用户已有的值）
env_default() {
  env_has "$1" "${3:-.env}" || env_set "$1" "$2" "${3:-.env}"
}
