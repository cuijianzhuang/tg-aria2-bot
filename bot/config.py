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
    download_dir: str = "/downloads"
    max_file_size: int = 2 * 1024 * 1024 * 1024
    max_concurrent: int = 3
    # 完成/失败时是否补发新消息（编辑已有进度卡片始终进行，编辑本身无推送打扰）
    notify_on_complete: bool = True
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

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    @staticmethod
    def _parse_ids(raw: str) -> set[int]:
        return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}

    @property
    def allowed_ids(self) -> set[int]:
        return self._parse_ids(self.allowed_user_ids)

    @property
    def admin_ids(self) -> set[int]:
        return self._parse_ids(self.admin_user_ids)

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        if self.admin_ids:
            return user_id in self.admin_ids
        # Fallback: env-seeded whitelist doubles as the admin list. An open bot
        # (both lists empty) has NO admins — admin features stay locked instead
        # of being world-writable.
        return user_id in self.allowed_ids


settings = Settings()
