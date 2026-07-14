#!/usr/bin/env bash
# Installs rclone natively on the host via the official installer, regardless of
# whether aria2 itself runs in a container (docker mode) or bare metal. rclone's
# official Linux binary is a statically linked Go executable (verified: no libc
# dependency), so the same host binary can be bind-mounted into an Alpine-based
# container without any glibc/musl issues.
#
# Shared by scripts/install_docker.sh and scripts/install_bare.sh so the install
# logic only lives in one place. Only installs the binary; configuring a remote
# (rclone config) is an interactive OAuth flow and is never automated here.
set -euo pipefail

log()  { printf '\033[1;32m[rclone]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$1" >&2; exit 1; }

if command -v rclone >/dev/null 2>&1; then
  log "rclone 已安装: $(rclone version | head -1)"
else
  log "通过官方脚本在宿主机安装 rclone (二进制装到 /usr/bin/rclone)"
  curl -fsSL https://rclone.org/install.sh | bash
  command -v rclone >/dev/null 2>&1 || die "rclone 安装失败，请查看上面的输出定位问题"
fi

warn "配置网盘 remote 需要交互式 OAuth 授权，无法自动化，请手动执行: rclone config"
