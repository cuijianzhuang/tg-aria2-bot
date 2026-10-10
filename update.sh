#!/usr/bin/env bash
# 兼容入口：升级逻辑已合并进 tg-aria2.sh，等同于 sudo ./tg-aria2.sh update ...
#
# 保留这个文件是因为老版本的 update.sh 升级完会 exec 仓库里的 update.sh 执行
# 第二阶段（--dir <仓库> --_apply ...）；带 --dir 时以它为准找 tg-aria2.sh。
dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
args=("$@")
for (( i = 0; i < ${#args[@]}; i++ )); do
  [[ "${args[$i]}" == "--dir" ]] && dir="${args[$((i + 1))]}"
done
exec bash "$dir/tg-aria2.sh" update "$@"
