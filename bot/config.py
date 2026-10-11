from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    api_id: int
    api_hash: str
    bot_api_url: str = "http://telegram-bot-api:8081"

    aria2_rpc: str = "http://aria2:6800/jsonrpc"
    aria2_secret: str

    allowed_user_ids: str = ""
    # Admin-only Telegram user ids (restart, whitelist management, gofile/rclone
    # toggles). Empty falls back to the env-seeded ALLOWED_USER_IDS — but never to
    # DB-added users, and never to "everyone" when the whitelist is off entirely.
    admin_user_ids: str = ""
    # 没有权限的用户收到的提示里"联系谁"。填 Telegram 用户名（如 @cuiplus）或任意
    # 文字/链接；留空则自动用管理员账号的用户名（管理员需要私聊过机器人）。
    admin_contact: str = ""
    # 在机器人里升级（/settings → ⬆️ 检查更新，仅管理员）。只有 bare 模式（git 仓库 +
    # systemd + root）支持；docker 模式请在服务器上运行 sudo tg-aria2 update。
    upgrade_enabled: bool = True
    # 定期检查有没有新版本，发现新版本时私聊通知管理员（同一个新版本只通知一次）。
    update_check_enabled: bool = True
    update_check_hours: int = 12
    # 没权限的用户可以点「申请使用」，管理员收到通知后一键通过/拒绝。被拒绝的人
    # access_request_cooldown_hours 小时内不能再申请。
    access_request_enabled: bool = True
    access_request_cooldown_hours: int = 12
    # 普通用户（非管理员）的默认额度，0 = 不限制。每个人可以用 /setquota 单独调整；
    # 管理员始终不受限制。每日额度按服务器时区的自然日统计。
    user_max_active_tasks: int = 0       # 同时进行的任务数
    user_max_task_size_gb: float = 0     # 单个任务大小上限
    user_daily_quota_gb: float = 0       # 每天下载总量
    download_dir: str = "/downloads"
    # 逗号分隔的备选下载目录，配合设置菜单里的"下载目录"切换使用；当前 download_dir
    # 始终排在选项第一位，这里只补充其它可选目录
    download_dir_presets: str = ""
    max_file_size: int = 2 * 1024 * 1024 * 1024
    max_concurrent: int = 3
    # 任务卡片进度刷新分两档：用户"正在看"的卡片（刚添加的任务、刚点过卡片上
    # 按钮的 progress_watch_seconds 秒内）按 progress_interval 快速刷新；其余
    # 后台下载的卡片按 progress_idle_interval 慢速刷新——没人盯着的 2 小时下载
    # 不需要编辑 2400 次。同一聊天里多张卡片会自动再放慢以避开 Telegram 的
    # 编辑频率限制。
    progress_interval: int = 2
    progress_idle_interval: int = 30
    progress_watch_seconds: int = 120
    # 完成/失败时是否补发新消息（编辑已有进度卡片始终进行，编辑本身无推送打扰）
    notify_on_complete: bool = True
    # 下载完成后自动把文件发回 Telegram（走自建 Bot API，上限 2GB；
    # 目录任务和超限文件自动跳过，只对单文件生效）
    auto_send_to_tg: bool = False
    # 磁盘剩余空间低于该值（GiB）时主动推送提醒管理员，0 表示关闭
    disk_alert_threshold_gb: int = 10
    # 已完成任务记录自动清理：保留天数，0 表示关闭；只删数据库记录，不动磁盘文件
    auto_cleanup_days: int = 0
    # ---- VIP 等级（需要 Pro 授权，见 LICENSE_KEY；没有启用授权校验的版本默认可用）----
    # VIP 用户的默认额度，0 = 不限制。个别用户仍可以用 /setquota 覆盖。管理员给 VIP：
    # /vip grant 用户ID 天数，或 /vip code 天数 生成兑换码发给用户，用户 /redeem 兑换。
    vip_max_active_tasks: int = 0
    vip_max_task_size_gb: float = 0
    vip_daily_quota_gb: float = 0
    # 只对 VIP 开放的功能，逗号分隔，可选 rss / node / gofile / autosend；留空 = 所有人可用
    vip_only_features: str = ""
    # VIP 的新任务插到 aria2 等待队列最前面
    vip_priority: bool = True
    # VIP 到期前几天提醒用户
    vip_warn_days: int = 3
    rss_max_feeds_vip: int = 20
    # 软件授权码（Pro）。也可以由管理员在机器人里 /license <授权码> 设置（存到 data/license.key）
    license_key: str = ""
    # 限速时段，如 "23:00-07:00=0;07:00-23:00=2M"（0 = 不限速，不在任何时段内时也不限速）。
    # 留空 = 不启用。管理员可以用 /schedule 在机器人里修改。
    speed_schedule: str = ""
    # 已完成任务的文件超过这么多天自动从磁盘删除（连同任务记录里的保存路径），0 = 关闭。
    # 删除前 FILE_EXPIRY_WARN_DAYS 天会提醒文件所有人。
    file_expiry_days: int = 0
    file_expiry_warn_days: int = 1
    # RSS 订阅：轮询间隔（分钟）、每人最多订阅几个源
    rss_check_minutes: int = 15
    rss_max_feeds_per_user: int = 5
    # 机器人 10 分钟内被启动 3 次以上，说明在反复崩溃重启，通知管理员
    restart_alert_enabled: bool = True
    proxy_url: str | None = None

    db_path: str = "/app/data/tasks.db"

    admin_password: str = ""
    web_port: int = 8080
    # Only honor X-Forwarded-For when the web admin actually sits behind a
    # trusted reverse proxy; when exposed directly, the header is
    # attacker-controlled and would let login rate limiting be bypassed.
    trust_proxy_headers: bool = False
    aria2_config_dir: str = "/aria2-config"

    # Where aria2.conf's on-download-complete hook should point when the rclone
    # upload toggle is used. Defaults match p3terx/aria2-pro's own convention
    # (it relocates clean.sh/core/etc to /config/script/ on first boot — see the
    # comment in aria2-config/aria2.conf). Bare-metal deployments must override
    # both to wherever their own aria2-bare/ directory actually lives, since there
    # is no /config/script/ split there.
    aria2_clean_hook: str = "/config/script/clean.sh"
    aria2_upload_hook: str = "/config/script/upload.sh"

    # gofile.io post-download pipeline (compress -> upload -> optionally delete
    # local copy). Independent of the rclone upload toggle above — this runs from
    # the bot process itself (task_manager.py), not an aria2 hook script, so
    # changes here need a bot restart, not an aria2 restart.
    gofile_enabled: bool = False
    gofile_token: str = ""
    gofile_compress: bool = True
    gofile_delete_local: bool = False

    # systemd unit names the web admin's "重启" button is allowed to restart —
    # bare-metal only (the web process needs `systemctl`, which docker containers
    # don't have; docker mode should leave these as-is and restart via
    # `docker compose restart bot`/`aria2` manually instead). The client only ever
    # sends the literal string "bot" or "aria2", never a raw unit name, so this
    # mapping is what keeps the restart endpoint from executing arbitrary input.
    bot_service_name: str = "tg-aria2-bot"
    aria2_service_name: str = "tg-aria2-bot-aria2"

    # extra="ignore"：.env 里出现本版本不认识的键（docker compose 自己读的
    # COMPOSE_PROFILES/HOST_DOWNLOAD_DIR、新版本才有的配置项、升级脚本回滚到
    # 旧版本后残留的新键）时忽略而不是启动即崩——pydantic-settings 对 env_file
    # 的默认策略是 forbid，升级/回滚场景下这等于一颗定时炸弹。
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @staticmethod
    def _parse_ids(raw: str) -> set[int]:
        return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}

    @property
    def allowed_ids(self) -> set[int]:
        return self._parse_ids(self.allowed_user_ids)

    @property
    def admin_ids(self) -> set[int]:
        return self._parse_ids(self.admin_user_ids)

    @property
    def admin_recipient_ids(self) -> list[int]:
        """要通知管理员时发给谁：明确配置的管理员优先，没配就用白名单
        （跟 is_admin 的回退口径一致）。"""
        return sorted(self.admin_ids or self.allowed_ids)

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        if self.admin_ids:
            return user_id in self.admin_ids
        # Fallback: env-seeded whitelist doubles as the admin list. An open bot
        # (both lists empty) has NO admins — admin features stay locked instead
        # of being world-writable.
        return user_id in self.allowed_ids

    def scope_for(self, user_id: int | None) -> int | None:
        """任务列表/搜索/统计的可见范围：管理员看全部（返回 None，调用方
        据此不加 user_id 过滤条件），普通用户只看自己的（原样返回 user_id）。"""
        if user_id is not None and self.is_admin(user_id):
            return None
        return user_id

    @property
    def download_dir_options(self) -> list[str]:
        """下载目录切换器里展示的候选目录：当前目录固定排第一（去重），
        其余来自 DOWNLOAD_DIR_PRESETS。"""
        options = [self.download_dir]
        for d in self.download_dir_presets.split(","):
            d = d.strip()
            if d and d not in options:
                options.append(d)
        return options


settings = Settings()
