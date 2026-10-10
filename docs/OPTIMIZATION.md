# 项目优化建议（全量代码审查）

> 审查范围：`bot/` 全部 Python 代码、Web 管理后台（前后端）、Docker / systemd / CI 部署配置。
> 日期：2026-07-16
>
> 优先级说明：
> - 🔴 高 —— 安全风险或在真实场景下会出错/丢数据的问题，建议尽快处理
> - 🟡 中 —— 稳定性 / 性能 / 可维护性问题，建议排期处理
> - 🟢 低 —— 打磨项，有空再做

---

## 一、安全类

### 🔴 1. 空白名单时，管理功能对所有人开放
`bot/middlewares/auth.py:24` —— `ALLOWED_USER_IDS` 为空时白名单完全关闭，机器人对任何 Telegram 用户开放。但 `bot/handlers/admin.py` 里的 `/adduser`、重启服务（`admin:restart:*`）、GoFile 开关、rclone 钩子切换等**管理级操作**也一并开放了。任何陌生人都能重启你的服务、往白名单里加自己。

**建议**：
- 引入独立的 `ADMIN_USER_IDS` 配置，管理类 handler（`admin.router`）单独挂一层管理员校验中间件；
- 或至少在白名单为空时禁用 admin router。

### 🔴 2. 任意白名单用户可操作他人任务，且回调数据未校验
Telegram 的 `callback_data` 是客户端可伪造的（改装客户端可发送任意字符串）：
- `bot/handlers/callbacks.py:196` `task:*:gid` 没有校验 `row["user_id"] == query.from_user.id`，任何白名单用户可暂停/删除/删文件他人的任务（多用户场景下是越权）；
- `bot/handlers/admin.py:151` `toggle_gofile` 中 `field = query.data.split(":")[3]` 直接拼成属性名 `setattr(settings, f"gofile_{field}", ...)`，伪造 `admin:gofile:t:token` 会把 `gofile_token` 覆盖成布尔值并写进 `.env`；未知字段则抛未捕获异常。
- `bot/handlers/admin.py:79` `int(query.data.split(":", 2)[2])` 遇伪造数据直接抛 `ValueError`。

**建议**：所有从 `callback_data` 解析出来的字段先做白名单校验（`field in {"enabled","compress","delete_local"}`）、`int()` 包 try、任务操作校验归属（或明确"共享实例，人人可管"并写进文档）。

### 🔴 3. Web 后台：密码明文比较 + 会话密钥复用密码
- `bot/web/app.py:126` / `:143` 用 `!=` 比较密码，存在时间侧信道，应改为 `hmac.compare_digest`；
- `bot/web/auth.py` 会话 token 直接用 `ADMIN_PASSWORD` 作 HMAC 密钥。拿到任意一个 cookie 的攻击者可以**离线爆破**密码（token 即 `expiry.HMAC(password, expiry)`，验证不需要访问服务器）。

**建议**：启动时生成/持久化一个随机 `SESSION_SECRET`（如存到 data 目录），HMAC 用它签名；密码改变时轮换 secret 即可保留"改密码踢掉所有会话"的语义。

### 🔴 4. 登录限流可被 X-Forwarded-For 头绕过
`bot/web/app.py:34` `_client_ip` 无条件信任 `X-Forwarded-For`。而 compose 里 8080 是**直接公网暴露、没有反代**的，攻击者每次请求换一个伪造的 XFF 就能完全绕过 5 次/5 分钟的登录限流，对 `ADMIN_PASSWORD` 无限爆破。

**建议**：默认取 `request.client.host`，仅当显式配置了 `TRUSTED_PROXY=true`（真的挂在反代后面）才解析 XFF；另外给 cookie 加 `secure=True`（配合下条 TLS）。

### 🟡 5. 管理后台明文 HTTP 公网暴露
`docker-compose.yml:54` 已有注释自知：无 TLS，`ADMIN_PASSWORD` 每次登录明文过网。建议文档中明确推荐挂 Caddy/Nginx + TLS（Caddy 两行配置即可自动签证书），或至少默认只绑 `127.0.0.1:8080` 由用户显式改公网。

### 🟡 6. Docker 模式下 Web 写 `.env` 是写进容器内的临时文件
`bot/web/app.py:17` `ENV_PATH = ".env"`，但 `web` 容器只挂载了 `./data` 和 `./aria2-config`，`.env` 是通过 `env_file` 注入的、并没有挂载进容器。结果：**改密码 / GoFile 设置在 docker 模式下写到容器内的 /app/.env，重启即丢**，而且 bot 容器根本看不到。裸机模式则依赖 CWD 恰好是仓库根目录，很脆。

**建议**：把可变配置统一放到 SQLite（已有 DB，加一张 `settings` 表）或挂载的 `data/` 目录下的独立文件；`.env` 只作首次引导。这同时解决"改 GoFile 设置要重启 bot"的问题（bot 定期/收到信号时重读 DB 配置即可）。

### 🟢 7. Bot token 落库
`bot/handlers/media.py:60` 生成的下载 URI 带 `bot<token>`，作为 `payload` 明文存进 `tasks` 表。DB 泄露即 token 泄露。可存相对 `file_path`，用时再拼 token。

---

## 二、正确性 / 稳定性

### 🔴 8. bot 和 web 两个进程共写同一个 SQLite，未开 WAL
`bot` 与 `web` 容器各自 `aiosqlite.connect(settings.db_path)` 打开同一个 `tasks.db`。默认 journal 模式下，一边写另一边写会随机报 `database is locked`（web 端清任务/加白名单撞上 bot 的 5 秒轮询写状态时必现概率不低）。

**建议**：`connect()` 后执行：
```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
```

### 🔴 9. 轮询循环是 N+1 RPC，且任务引用未持有
`bot/core/task_manager.py`：
- `_poll_once` 对每个未完成任务单独 `get_status(gid)`（每 5 秒 N 次 RPC + N 次线程切换）。aria2 一次 `tellActive/tellWaiting/tellStopped` 就能拿全量（`get_progress_map` 已经这么做了）——改为拉一次全量、按 gid 匹配，任务多时开销从 O(N) 降到 O(1)；
- `start()` 里 `asyncio.create_task(...)` 的返回值没有保存。CPython 的事件循环只弱引用 task，**轮询任务可能被 GC 静默回收**（asyncio 文档明确警告）。`_run_gofile_pipeline` 的 create_task 同理。保存到 `self._poll_task`，`stop()` 时 `cancel()` 并 await；
- `_last_edit` 字典只增不删，长期运行内存缓慢增长——任务进入终态时 `pop(gid, None)`。

### 🟡 10. gid 丢失一律标 FAILED，会误伤已完成任务
`task_manager.py:60` 与 `reconcile_on_startup` 注释里也承认了这个歧义。可以廉价地消歧：DB 里有 `save_path`/`file_name`，标 FAILED 前先 `os.path.exists()` 探测一下，文件在就标 COMPLETED，显著减少"明明下完了却显示失败"的用户困惑。

### 🟡 11. `pending:start` 双击竞态会重复添加下载
`bot/handlers/callbacks.py:146`：`get_pending` → 添加 aria2 → `delete_pending`。快速双击"开始下载"两次都能读到 pending，产生两个下载任务，且第二次 `create_task` 会撞 `gid UNIQUE`… 不会，gid 不同，会插两条。**建议**先 `pop_pending`（原子删除并返回），添加失败再写回（或让用户重发，成本很低）。

### 🟡 12. `write_kv` 非原子写，崩溃即损坏配置
`bot/core/conf_editor.py:44` 直接原地覆写 `aria2.conf` / `.env`。进程在 `f.writelines` 中途被杀（重启按钮恰好会重启自己！`admin.py:227`），配置文件截断。**建议**：写临时文件 + `os.replace()` 原子替换；`.env` 与 conf 的并发写再加一把 `asyncio.Lock`。

### 🟡 13. 进度编辑节流对 Telegram 限速仍偏激进
`task_manager.py:149` 条件是"间隔<3s **且** 增量<5%"才跳过——也就是每个任务最快 3 秒编辑一次。同一 chat 内 Telegram 编辑上限约 20 次/分钟，3 个以上并发任务就会开始吃 429（当前 `except Exception: pass` 把 429 也吞了，表现为进度卡住）。**建议**：把最小间隔提到 ~10s，并按 chat 维度做全局预算；捕获 `TelegramRetryAfter` 按 `retry_after` 退避而不是裸吞。

### 🟡 14. aria2 RPC 无超时兜底
`Aria2Client` 走 `asyncio.to_thread` + aria2p（requests 同步）。aria2p 默认 timeout 有限但较长，aria2 卡死时轮询线程会长时间悬挂。建议构造 `aria2p.Client(..., timeout=10)` 显式设短超时。

### 🟢 15. 其它小项
- `bot/core/aria2_client.py:61` `remove` 先 `get_status` 再删，两次往返；`client.remove(gid)` 一次即可（files=True 时才需要 files 列表）；
- `tell_active()` 实际返回全部状态的下载（`get_downloads`），命名误导，建议改名 `get_all_downloads`；
- `bot/core/gofile.py:53` 在事件循环里用同步文件对象喂 aiohttp FormData，大文件上传时读盘阻塞事件循环，建议用 `aiofiles` 或分块 reader；每次调用都新建 `ClientSession`，可复用一个模块级 session；
- `repo.update_status` 把任务从终态改回 ACTIVE 时旧 `finished_at` 不清（`COALESCE` 保留旧值），重试路径靠 `retry_task` 单独清，容易漏；
- `commands.py` 的 `/pause`、`/resume` 只调 aria2 不更新 DB 状态（靠轮询兜底，5 秒内 UI 不一致），与 callbacks 里的行为不对称。

---

## 三、性能与数据库

### 🟡 16. 缺索引
`tasks` 表的高频查询模式是 `WHERE status = ?` 和 `ORDER BY created_at DESC`（`list_recent`、`count_by_status`、`get_unfinished`），目前全表扫。任务记录累积几千条后 Web 列表和 `/list` 会变慢：

```sql
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
```

放进 `MIGRATIONS`（`CREATE INDEX IF NOT EXISTS` 天然幂等，比 ALTER TABLE 还省事）。

### 🟢 17. 长期：aria2 RPC 改原生异步
aria2p 是同步库，每次调用都要 `to_thread`。aria2 的 JSON-RPC 极简单，用 aiohttp 直接 POST（或 websocket，还能收 `onDownloadComplete` 推送、**彻底去掉 5 秒轮询**）是更彻底的方案。事件驱动后完成通知也从"最多迟 5 秒"变成即时。工作量中等，收益是删掉整个轮询节流体系的复杂度。

---

## 四、工程化 / CI / 部署

### 🔴 18. CI 只在部署时跑测试，部署脚本缺依赖安装
`.github/workflows/deploy.yml`：
- 测试是在**生产机上、git reset --hard 之后**才跑的——测试挂了代码已经换掉了，只是服务没重启，处于半更新状态；
- 没有 `pip install -r requirements.txt`，任何依赖变更都会让部署直接挂；
- 只重启 `tg-aria2-bot`，`tg-aria2-web` 永远跑旧代码。

**建议**：加一个 PR/push 触发的 test workflow（`pip install && python -m unittest discover tests`，可加 `ruff check`），deploy job `needs: test`；部署脚本补 `pip install -r requirements.txt` 和 web 服务重启。

### 🟡 19. 测试覆盖
现有 `tests/test_cards.py` 只覆盖纯渲染函数。性价比最高的补充（都不需要真实网络）：
- `conf_editor`：`write_kv` 的注释保留/取消注释/追加行为，`is_safe_value` 边界；
- `web/auth.py`：token 过期、篡改签名、非数字 expiry；
- `TaskRepo`：用内存 SQLite（`:memory:`）测状态机、dedup 索引、pending TTL 清理；
- `storage.build_subdir` / `url_hash`。

### 🟡 20. Docker 镜像
`Dockerfile`：
- 缺 `.dockerignore`（当前 build context 会把 `downloads/`、`vendor/`、`data/` 全部打包发给 daemon，vendor 里还有个 `core` 文件）；
- 以 root 运行，建议 `USER` 非特权用户（compose 里 aria2 已经在用 PUID 1000，对齐即可）；
- 基镜像 `python:3.11-slim` 建议 pin digest 或至少 minor 版本；
- `docker-compose.yml:1` 的 `version: "3.8"` 字段已废弃，可删。

### 🟢 21. 依赖版本
全部精确 pin 是好事，但建议定期升级（aiogram 3.13 → 3.x 最新有 bugfix；aiohttp/fastapi 有安全更新节奏）。可加 Dependabot/Renovate 配置自动提 PR，配合上面第 18 条的 CI 测试兜底。

### 🟢 22. 代码质量工具
项目无 lint/format 配置。加一个最小 `pyproject.toml`（`ruff` + `ruff format`）成本极低，也给 CI 一个静态检查步骤。现有代码风格已经相当一致，引入阻力小。

---

## 五、结构与可维护性（低优先级）

- **`callbacks.py` 偏胖**（440 行、承载导航/列表/任务操作/文件选择/批量操作五类职责）。可按 `nav_`/`list_`/`task_`/`filesel_` 拆成多个 Router，模式已经有了（admin.py 就是独立 router）；
- **状态字符串散落各处**：`models.STATUSES` 自称"single source of truth"，但 `_mapped_status`、`task_manager._handle_download_state`、`list_view.LIST_STATUS_MAP` 各写各的映射。抽一个 `map_aria2_status(s) -> TaskStatus`（`enum.StrEnum`）统一；
- **Web 前端 `app.js`（334 行）无构建、无框架**——这个体量完全合理，不建议引入构建链，保持现状即可；
- **中文文案硬编码**：单用户自部署场景没问题，不建议为 i18n 增加复杂度；
- **repo 每次操作都 `commit()`**：SQLite 下没问题，但如果做批量操作（bulk pause 1000 个任务 = 1000 次 commit）会慢，可给 bulk 路径加事务。

---

## 建议的处理顺序

| 批次 | 内容 | 预估工作量 |
|------|------|-----------|
| 1（安全） | #1 管理员权限隔离、#2 回调校验、#3 会话密钥、#4 XFF 限流绕过 | 半天 |
| 2（稳定） | #8 WAL、#9 轮询重构+任务引用、#12 原子写、#16 索引 | 半天 |
| 3（工程） | #18 CI 测试前置 + 部署补全、#20 dockerignore/非 root | 半天 |
| 4（体验） | #10 FAILED 误报消歧、#11 双击竞态、#13 429 退避、#6 配置持久化 | 1 天 |
| 5（长期） | #17 原生异步 RPC / websocket 事件驱动、#19 测试补全、拆分 callbacks | 按需 |

---

## 实施记录（2026-07-16，本分支）

| 项 | 状态 | 说明 |
|----|------|------|
| #1 管理员隔离 | ✅ | 新增 `ADMIN_USER_IDS` + `AdminMiddleware` 挂在 admin router；空白名单时管理功能锁定而非全开 |
| #2 回调校验 | ✅ | gofile 字段白名单、deluser 安全解析、task/pending/filesel/bulk/cleanup 归属校验（owner-or-admin） |
| #3 会话密钥 | ✅ | 随机 secret 持久化于 DB 目录（0600），改密码时轮换；密码比较改 `compare_digest` |
| #4 XFF 绕过 | ✅ | 新增 `TRUST_PROXY_HEADERS`，默认不信任 XFF |
| #5 TLS | ⏳ 未做 | 部署侧动作，建议挂 Caddy/Nginx |
| #6 配置持久化 | ✅（方案调整） | 未迁移 DB，改为把 `.env` bind mount 进 bot/web 容器，写回即持久 |
| #7 token 落库 | ✅ | `tg_media` 的 payload 改存 Telegram 原始 `file_path`（不含 token），真正的下载 URI 只在 `_add_source` 调用 aria2 那一刻现拼；`url`/`magnet`/`torrent` 本来就不含密钥 |
| #8 WAL | ✅ | `journal_mode=WAL` + `busy_timeout=5000` + `synchronous=NORMAL` |
| #9 轮询重构 | ✅ | 单次全量 RPC 匹配 gid；持有 poll/gofile 任务强引用并在 stop 时取消；`_last_edit` 终态清理 |
| #10 FAILED 误报 | ✅ | gid 丢失时先探测磁盘文件，存在则标 COMPLETED |
| #11 双击竞态 | ✅ | `pop_pending` 原子化（DELETE..RETURNING，含降级路径），失败可 `restore_pending` |
| #12 原子写 | ✅ | temp + `os.replace`，对单文件 bind mount 回退原地写 |
| #13 429 退避 | ✅ | 最小编辑间隔 3s→10s；捕获 `TelegramRetryAfter` 按 chat 退避 |
| #14 RPC 超时 | ✅（后续随 #17 重写为原生异步后更新，见 2026-07-27 记录） | 当时是 aria2p Client timeout=10s；`aria2_rpc.py` 重写后改成 `aiohttp.ClientTimeout(total=10)`，同一超时兜底 |
| #15 小项 | ✅ | `tell_active`→`get_all_downloads`；`/pause` `/resume` 同步 DB 状态；gofile 模块改用进程级复用的 `aiohttp.ClientSession`（原来每次上传开 3 个新 session），进程退出时 `close_session()` 收尾 |
| #16 索引 | ✅ | `idx_tasks_status`、`idx_tasks_created` |
| #17 异步 RPC | ✅ | 新增 `bot/core/aria2_rpc.py`（纯 aiohttp JSON-RPC + WebSocket 事件订阅），`aria2_client.py` 全部重写为原生异步（不再是 aria2p+to_thread），移除 aria2p 依赖；TaskManager 每个节点常驻一条 WS 连接，收到 onDownloadComplete/onBtDownloadComplete/onDownloadError 立即处理那个 gid，5 秒轮询降级为兜底而非唯一路径；顺带修了一个真实 bug：`remove(files=True)` 在远程节点上不再误删 bot 本机同名路径的文件 |
| #18 CI | ✅ | 新增 test workflow；deploy 依赖 test job，服务器端补 `pip install` 和 web 重启 |
| #19 测试补全 | ✅ | 新增 web/auth、conf_editor、TaskRepo 测试（42 个用例） |
| #20 Docker | ✅ | `.dockerignore`、去掉 compose `version`；非 root 用户（UID/GID 1000，跟 aria2 的 PUID/PGID 对齐）——`install.sh` 新装机自动 `chown` 好 bind mount 目录/`.env`，旧部署升级需要手动跑一次 `chown`（README 已写明），未在真实 Docker 环境里跑通全流程（沙箱没有 docker daemon），建议合并后在真实环境验证一次 |
| #21 依赖升级 | ✅ | 新增 `.github/dependabot.yml`（pip / github-actions / docker，每周检查） |
| #22 ruff | ✅ | 新增 `pyproject.toml`（E/F/I/B/UP 规则集，行长交给软限制不强拆），修了 42 处历史违规（大多是 `datetime.UTC` 别名、import 排序这类风格问题），CI 新增 `ruff check` 步骤；`ruff format` 的全量重排风格差异太大，未启用 |

## 实施记录（2026-07-27，本分支）

在按用户过滤任务列表/统计（`user_id` 可见范围隔离）上线后追加的两处收尾：

| 项 | 状态 | 说明 |
|----|------|------|
| #16 索引补充 | ✅ | 新增 `idx_tasks_user_created(user_id, created_at DESC)`、`idx_tasks_user_status(user_id, status)`，覆盖按用户过滤后的列表/搜索排序查询和计数/统计查询；原有 `idx_tasks_status`/`idx_tasks_created` 继续覆盖管理员的全量查询 |
| `Aria2Client.remove` 冗余往返 | ✅ | 原来无论 `files` 参数是否为真都会先 `tellStatus` 拿一次 dir/files，只有 `files=True` 且是本地节点才用得上；改成只在真需要删文件时才发这次 RPC，顺带修了一个小回归：之前对一个已经不存在的 gid 调用 `remove(gid, files=False)`（比如取消已被外部清理的任务）会被这次多余的 `tellStatus` 提前抛错，现在会正常走到 `forceRemove`/`removeDownloadResult` 的兜底逻辑 |
| #14 补充：轮询串行拖慢 | ✅ | `_poll_once` 原来是逐节点 `for` 循环——单次 RPC 有 10s 超时兜底没错，但串行意味着一个卡住的节点会让排在它后面的健康节点也多等最多 10s，节点越多越明显；改成 `asyncio.gather` 并发展开各节点，同时 `return_exceptions=True` 顺带修了另一个问题：某节点处理某个任务行时抛出非 RPC 层异常，以前会打断当轮循环里排在它后面的其它节点，现在互不影响 |
| 节点离线告警 | ✅ | `TaskManager._handle_node_health`：节点连续不可达超过 10 分钟（`NODE_OFFLINE_ALERT_SECONDS`）才告警一次管理员，跟磁盘告警同一套冷却/重置语义；见 `docs/MULTI_NODE_DESIGN.md` §七 |

## 实施记录（2026-10-07）：部署与升级流程

| 项 | 说明 |
|----|------|
| `.env` 未知键导致启动即崩 | pydantic-settings 对 `env_file` 默认 `extra="forbid"`：`.env` 里只要出现当前版本不认识的键（新版本的配置项、回滚到旧版本后残留的键、给 compose 用的变量）bot 直接起不来。改为 `extra="ignore"`，补了回归测试 |
| `install.sh` 重跑冲掉运行时配置 | 以前每次整个重写 `.env`，设置菜单/Web 后台写回的 `MAX_CONCURRENT`、`GOFILE_*`、`AUTO_CLEANUP_DAYS` 等全部丢失。改为首次从 `.env.example` 生成、之后只就地改脚本管理的键（`scripts/env_lib.sh`）；重跑时凭据/模式从 `.env` 预填，不用再输入 |
| bare 模式服务名/钩子路径错位 | `.env` 默认 `ARIA2_SERVICE_NAME=tg-aria2-bot-aria2`、钩子路径是 docker 的 `/config/script/`，但 aria2.sh 注册的服务名是 `aria2`、配置在 `/root/.aria2c`——"重启 aria2"按钮和 rclone 钩子切换在 bare 模式下都指向不存在的东西。首次 bare 安装时写入正确值 |
| docker `--download-dir` 不生效 | compose 里写死 `./downloads`，`--download-dir` 只是在宿主机建了个没挂载的目录。改为 `${HOST_DOWNLOAD_DIR:-./downloads}` |
| 新增 `update.sh` | 一键升级：fetch → 列出提交 → 备份（.env、SQLite 在线备份、版本号）→ 保存/恢复 `aria2-config/` 本地改动（以前它们会让 `git pull` 直接失败）→ ff 更新 → 两阶段 exec 新版脚本应用 → 健康检查（20s 内是否崩溃重启）→ 失败自动回滚。bare 模式重启前先做导入校验，systemd 单元只在模板变化时同步且先备份。在沙箱里模拟了正常升级/导入失败/运行时崩溃/从无 update.sh 的老版本首次升级四种场景 |
| CI/CD | deploy 复用 test.yml（以前 deploy 自带的测试 job 漏了 ruff 和导入检查），服务器端改走 `update.sh --reset`，加 `concurrency` 防并发部署；test 增加 Python 3.14（docker 镜像实际版本）矩阵和 shellcheck |
| `deploy.sh` | 同步了 `requirements.txt` 却从不 `pip install`、不重启 web；补上，目标服务器可用环境变量覆盖 |
| docker | 所有服务日志轮转（10MB×3）；`COMPOSE_PROFILES` 写进 `.env`，手敲 `docker compose up` 不再漏掉 web；`WEB_BIND` 可把 8080/6880 改为只监听本机；README 里"只监听 127.0.0.1"的说法与 docker 实际（对公网开放）不符，已更正 |
| 交互式管理菜单 | 新增 `manage.sh`（安装时注册快捷命令 `tg-aria2`）：服务状态/日志/重启/启停、常用配置修改（带校验，改密码同时轮换会话密钥）、Web 访问信息、检查更新/升级、备份/恢复、回退历史版本；docker/bare 两种模式同一套操作，也支持子命令非交互调用。`update.sh` 配套新增 `--to`（回退到指定版本，复用备份/健康检查/自动回滚）和 `--backup-only` |
| docker 下设置被重启还原 | bot/web 容器同时用 `env_file: .env`（创建时冻结成环境变量）和挂载的 `/app/.env`，而 pydantic-settings 中环境变量优先于 .env 文件——设置菜单写回 .env 的开关（GoFile、自动发送等）在 `docker compose restart` 后被创建时的旧值盖掉、悄悄还原。去掉 bot/web 的 `env_file`，只从挂载文件读配置 |

## 实施记录（2026-10-07）：Telegram 按钮交互

| 项 | 说明 |
|----|------|
| 🔴 设置页越权 | 主菜单「⚙️ 设置」进入的全局设置回调（限速、并发、单文件上限、下载目录、自动清理、通知/自动发送）都挂在普通 router 上，没有管理员校验：`/settings` 命令是管理员专属，但任何白名单用户点按钮就能改全局配置、触发清理。拆到 `handlers/settings_menu.py` 并整体挂 `AdminMiddleware`，非管理员的主菜单不再显示「设置」，任务列表不显示「清理已完成」 |
| 返回列表丢位置 | 列表里点开任务后「返回列表」固定回「全部」第一页；在卡片上暂停/刷新一下返回按钮就消失。列表行改为 `topen:<tab>:<页>:<gid>`，返回按钮在卡片和各级子菜单之间一路保留 |
| 任务卡片按钮 | 「ℹ️ 详情」实为刷新，改名「🔄 刷新」；「📂 位置」实为选择文件，改名「🗂 选择文件」且确定是单文件时不显示；已完成任务的 GoFile 链接改为直接打开的 URL 按钮，去掉与「保存位置」重复的「获取链接」；失败卡片去掉「查看原因」（原因已在正文）；已取消任务可「重新下载」 |
| 删除确认 | 「删除」以前一点就删、无确认。改为确认页：仅删记录 / 删记录和文件（本机节点、路径确实在下载目录内才出现，删文件二次确认；伪造回调也无法删到下载目录之外或目录本身）。从列表进来的删完回到列表原位置 |
| 列表/主菜单 | 列表行带状态图标、加刷新按钮；批量「全部暂停/继续」分别只出现在对应 tab；主菜单加「📊 统计」，有失败任务时加「⚠️ 失败 N」直达 |
| 选择器 | 全局限速选择器标出当前值（aria2 返回字节数，换算后对上预设）；限速/单文件上限按钮改为多列排布，少占屏幕 |
| GoFile 流水线 | 上传前就失败时完成通知会因 `link` 未定义抛 NameError（新加的链接按钮会触发），先初始化 |

## 实施记录（2026-10-10）：脚本合并

| 项 | 说明 |
|----|------|
| 合并为单脚本 | `install.sh` / `scripts/install_docker.sh` / `scripts/install_bare.sh` / `scripts/install_rclone.sh` / `update.sh` / `manage.sh` / `scripts/env_lib.sh` / `scripts/net_lib.sh` 合并为 `tg-aria2.sh`（菜单 + `install` / `web-install` / `update` / `status` / `config` / `backup` … 子命令）。`install.sh`、`update.sh` 保留为转发入口：老版本的 update.sh 升级完会 exec 仓库里的 update.sh 执行第二阶段，必须还能用；CI 改为直接取 origin/master 上的 tg-aria2.sh |
| 安装中途失败 | 以前任何一步出错 `set -e` 直接退出，后面的 bot/Web 服务都没装、也不说是哪步。现在每步有名字，失败时报告「在哪一步失败」+ 当前各服务状态 + 重跑即可（已完成的跳过）；菜单顶部检测到核心服务缺失会提示修复；新增 `web-install` 单独装 / 修 Web 后台 |
| Python 环境 | Debian/Ubuntu 的 python3 默认不带 venv（ensurepip），`python3 -m venv` 失败是最常见的安装中断原因，现在自动装 `python3.x-venv`；bot 代码需要 Python ≥ 3.11（`datetime.UTC` 等），以前不检查，3.9/3.10 的系统装完服务起不来，现在先找 ≥3.11 的解释器，没有就尝试装 `python3.11`，再不行明确提示改用 docker 模式；装完先 `import bot.main` 验证再注册服务 |
| AriaNg 非关键 | 下载 AriaNg 要访问 GitHub，失败以前会中断整个安装，现在只警告 |
| `--botapi-from-source` | 以前只有 install_bare.sh 认识这个参数，从 install.sh 根本传不进去，现在是 install 的正式选项 |
| systemd 单元同步 | 升级时以前只要 `systemd/` 目录有任何变化就重写所有单元（模拟里发现：只改了 telegram-bot-api.service 的模板，tg-aria2-bot.service 也被覆盖），改为逐个单元按自己的模板是否变化判断 |
