# 多节点控制设计文档

> 目标：一个 bot 同时管理多个 aria2 部署实例（下称"节点"），用户可随时切换
> "当前节点"，新任务落到当前节点，所有节点上的任务统一轮询、统一在列表里展示。
>
> 状态：设计稿，待确认后实施。

---

## 一、背景与现状

当前代码从上到下都假定**只有一个 aria2**：

| 位置 | 单节点假定 |
|------|-----------|
| `bot/config.py` | `aria2_rpc` / `aria2_secret` / `download_dir` 各只有一份 |
| `bot/main.py` | 只建一个 `Aria2Client`，`dp["aria2"]` 注入所有 handler |
| `bot/core/task_manager.py` | 轮询循环只拉一个节点的 `get_all_downloads()` |
| `bot/db/models.py` | `tasks` 表没有节点归属列，gid 全局唯一的前提只在单节点内成立 |
| `bot/handlers/callbacks.py` | `_add_source` 直接用注入的单个 client |
| 磁盘检查 / 发送到TG / GoFile / 服务器状态 | 全部读**本机**文件系统，隐含"aria2 和 bot 在同一台机器" |

典型使用场景：家里群晖跑一个 aria2（大容量存储）+ 云服务器跑一个 aria2（大带宽公网），
希望在同一个 bot 里按需选择往哪边下。

---

## 二、核心概念

### 节点（Node）

一个可被 RPC 访问的 aria2 实例。字段：

```
name          唯一名称（如 "云服务器" / "群晖"），按钮和卡片上显示
rpc_url       http://host:6800/jsonrpc
secret        RPC 密钥
download_dir  该节点上的默认下载目录（远端路径，bot 不假定能访问）
is_local      bot 进程是否与该节点同机/同文件系统（决定能否用本地能力，见 §六）
enabled       停用后不轮询、不可选择，但历史任务仍可查看
```

### 默认节点（向后兼容的关键）

现有 `.env` 的 `ARIA2_RPC`/`ARIA2_SECRET`/`DOWNLOAD_DIR` 自动注册为内置节点
`default`（显示名"本机"，`is_local=true`，不可删除）。**不配置任何额外节点时，
行为与现在完全一致**——单节点用户升级后零感知。

### 当前节点（per-user）

每个用户有自己的"当前节点"选择（存 DB，重启不丢）。新任务落到发起人当时的
当前节点。不做全局当前节点——多用户场景下 A 切节点不应该影响 B 正在发的任务。

---

## 三、数据模型

```sql
-- 新表：额外节点（default 节点来自 .env，不入库）
CREATE TABLE IF NOT EXISTS nodes (
    name          TEXT PRIMARY KEY,
    rpc_url       TEXT NOT NULL,
    secret        TEXT NOT NULL,
    download_dir  TEXT NOT NULL DEFAULT '/downloads',
    is_local      INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 新表：用户偏好（目前只有当前节点，后续偏好也放这里）
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id       INTEGER PRIMARY KEY,
    current_node  TEXT NOT NULL DEFAULT 'default'
);

-- tasks 表加节点归属（迁移，旧行全部归 default，语义正确）
ALTER TABLE tasks ADD COLUMN node TEXT NOT NULL DEFAULT 'default';

-- pending_tasks 同理：确认卡片创建时记下目标节点，点"开始下载"时用它路由，
-- 避免"发链接后切了节点再点开始"落错地方
ALTER TABLE pending_tasks ADD COLUMN node TEXT NOT NULL DEFAULT 'default';
```

**gid 唯一性**：aria2 的 gid 是各实例独立生成的 64bit 随机数，跨节点碰撞概率
可忽略但不为零，而且 `tasks.gid` 有 UNIQUE 约束。方案：查询任务时一律
`WHERE gid = ? AND node = ?` 双键定位（callback_data 里已有 gid，node 从
tasks 行反查，不用改 callback 格式）；`gid` 的 UNIQUE 约束保留（碰撞时插入
失败重试添加即可，实际上不会发生）。**callback_data 不需要塞 node**，这是
本设计能控制住改动面的关键。

**密钥存储**：额外节点的 secret 存 SQLite。与现状（secret 明文在 .env）风险
等级相同——DB 文件和 .env 在同一台机器同一目录，不引入新的暴露面。

---

## 四、核心组件改造

### 1. NodePool（新，`bot/core/node_pool.py`）

```python
class NodePool:
    async def load(repo)                 # 启动时：default(.env) + nodes 表 → 建 Aria2Client
    def get(name) -> Aria2Client         # 未知/停用节点抛 NodeUnavailable
    def get_node(name) -> Node           # 节点元信息（download_dir、is_local…）
    def all_enabled() -> list[Node]
    async def add / remove / set_enabled # 管理操作，同步写 nodes 表
    async def health(name) -> bool       # aria2.getVersion，超时 5s
```

替换 `dp["aria2"]` 为 `dp["nodes"]`（NodePool）。为兼容现有 handler 签名，
过渡期同时保留 `dp["aria2"] = pool.get("default")`，逐个 handler 迁移完再删。

### 2. TaskManager 轮询

```
for node in pool.all_enabled():
    downloads = await pool.get(node.name).get_all_downloads()   # 异常→跳过该节点
    rows = await repo.get_unfinished(node=node.name)
    ...现有 per-row 状态机不变...
```

- 单个节点挂掉只影响它自己：捕获异常、记一次 log、该轮跳过，**不把该节点的
  任务标 FAILED**（节点不可达 ≠ 任务丢失；连续不可达超过阈值再告警，见 §七）。
- `reconcile_on_startup` / `_mark_lost` 同样按节点分组；`_mark_lost` 的
  "磁盘上文件还在就标 COMPLETED" 探测只对 `is_local` 节点做（远端文件系统摸不到）。

### 3. 任务路由

- `links.py` / `media.py` 创建 pending 时写入发起人的 `current_node`；
- `_add_source(pool, node, kind, payload, ...)` 增加 node 参数，client 和
  download_dir 都从该节点取；
- 重试（task:retry）沿用任务记录里的 node，不跟随用户当前节点。

---

## 五、交互设计

### 节点切换（所有白名单用户可用）

- 主菜单新增一行：`🖥 节点: 本机 ▾`（显示当前节点名），点开节点选择器：

```
🖥 选择下载节点
──────────
·🟢 本机·          ← 当前节点加点标记，🟢/🔴 为健康状态
 🟢 云服务器
 🔴 群晖 (离线)     ← 离线节点可见但点击提示不可用
──────────
⬅️ 返回
```

- 确认卡片（发链接后）上显示目标节点：`📍 节点：云服务器`，并加一个
  `🖥 切换节点` 按钮——发之前临时改主意不用先去主菜单。

### 节点管理（仅管理员，挂 admin router 自动继承权限校验）

- 设置菜单新增 `🖥 节点管理`：列出全部节点（健康状态、任务数），每个节点有
  启用/停用、删除（default 不可删）按钮；
- 添加节点用命令：`/addnode 名称 rpc_url secret [download_dir]`
  - secret 出现在聊天消息里，处理完成后 bot **立即删除该条消息**（失败则提示
    用户手动删）；
  - 添加前先做一次 health check，连不上直接拒绝并给出原因，避免存入废节点。

### 展示

- 任务卡片、任务列表行、搜索结果：多于一个节点时显示节点名（如
  `📍 云服务器`）；只有 default 一个节点时**不显示**，避免单节点用户界面变啰嗦；
- `/stats`：默认聚合全部节点，周期行下加一行按节点拆分（任务数/流量）；
- 服务器状态页：本机指标（/proc）只代表 bot 所在机器，页面上注明；每个节点
  的 aria2 全局速度/活动数逐节点列出。

---

## 六、能力矩阵（本地 vs 远程节点）

这些功能依赖 bot 进程能直接访问下载产物所在的文件系统，远程节点上不可用，
**按钮直接不渲染**（而不是点了报错）：

| 功能 | 本机节点 | 远程节点 | 说明 |
|------|---------|---------|------|
| 下载 URL/磁力/种子 | ✅ | ✅ | 种子文件 bot 读本地副本后用 `add_torrent` 传字节，天然跨节点 |
| tg_media（转存 TG 文件） | ✅ | ⚠️ 默认禁用 | file:// URI 只在本机有效；远程节点需要它能回连 bot-api 的 HTTP 地址，默认不假定连通。Phase 2 可加 `BOT_API_PUBLIC_URL` 配置显式开启 |
| 📤 发送到 TG / 自动发送 | ✅ | ❌ | 文件在远端磁盘 |
| GoFile 压缩上传 | ✅ | ❌ | 同上 |
| 磁盘空间检查/告警 | ✅ | ❌* | *aria2 RPC 不暴露磁盘信息；远程节点跳过预检，靠 aria2 自身下载失败兜底 |
| 丢失 gid 的磁盘探测 | ✅ | ❌ | 远程节点 gid 丢失一律按 FAILED 处理（回到旧行为） |
| 暂停/继续/取消/限速/文件选择/进度 | ✅ | ✅ | 纯 RPC，全部可用 |

---

## 七、健康监控

- NodePool 缓存每个节点最近一次轮询成功/失败状态 + 时间戳；
- 节点选择器/管理页的 🟢🔴 直接读缓存，不现场探测（避免打开菜单卡几秒）；
- 连续不可达超过 10 分钟 → 复用现有 `_notify_admins` 私聊告警一次（与磁盘
  告警相同的冷却机制：恢复后重置，再次跌破再报）。

---

## 八、实施计划

**Phase 1（核心，一个 PR）**
1. 数据模型：`nodes`/`user_prefs` 表 + `tasks`/`pending_tasks` 加 `node` 列（迁移）
2. `NodePool` + main.py 装配；TaskManager 按节点轮询、错误隔离
3. 任务路由：pending 记录节点、`_add_source` 按节点取 client/dir、重试沿用原节点
4. 交互：主菜单节点切换器、确认卡片显示节点、`/addnode` + 节点管理页（admin）
5. 能力矩阵落地：远程节点隐藏 sendtg/gofile 相关按钮、跳过磁盘检查、tg_media 锁定本机
6. 展示：卡片/列表/搜索带节点名（多节点时）
7. 测试：NodePool 装配与降级、路由落点、迁移升级路径、矩阵开关

**Phase 2（可选增强）**
- 节点健康告警（§七的连续不可达告警）
- `/stats` 按节点拆分
- 远程节点 tg_media（`BOT_API_PUBLIC_URL`）
- Web 后台的节点维度展示
- 每节点独立的目录预设（当前 `DOWNLOAD_DIR_PRESETS` 只对本机有意义）

**明确不做**
- 节点间任务迁移/文件同步（rclone 的职责，不是 bot 的）
- 自动选节点/负载均衡（家用场景想选哪就选哪，自动化反而添乱）

---

## 九、风险与开放问题

1. **`settings.download_dir` 的残留引用**（约 27 处）：Phase 1 会把"任务落
   盘目录"全部改为从节点取，但"本机能力"类引用（磁盘告警、服务器状态）保留
   本机语义——review 时需逐处确认归类正确，这是本次改动最大的出错面。
2. **web 管理后台**只连 default 节点（它自己建 `Aria2Client`），Phase 1 不动，
   页面上标注"仅本机节点"。
3. **待确认**：
   - 节点切换权限给所有白名单用户还是仅管理员？（设计按"所有人"，因为
     current_node 是个人偏好、不影响他人）
   - `/addnode` 的消息即焚方案可接受吗？另一个选择是只允许在 web 后台添加
     节点（避免 secret 进聊天记录，但要多做一个 web 表单）。
