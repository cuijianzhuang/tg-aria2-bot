# tg-aria2-bot

Telegram 下载机器人：给机器人发送 **HTTP(S) 链接 / 磁力链接 / .torrent 文件 / 转发媒体消息**，
由服务器上的 aria2（[P3TERX 完美配置](https://github.com/P3TERX/aria2.conf)）执行下载，
机器人以卡片形式实时回报进度，并可选压缩上传 GoFile 网盘。附带 Web 管理后台 + AriaNg 面板。

## 功能特性

**Telegram 交互（卡片式，全按钮操作）**

- **确认后下载**：发送链接/种子后先出确认卡片（文件名/大小/保存位置），点「▶️ 开始下载」才真正入队，防误触
- **实时任务卡片**：进度条、速度、剩余时间、连接数、保存路径，原地刷新不刷屏（3 秒/5% 节流）
- **任务列表**：单页浏览，顶部分段式筛选 tab（全部/下载中/等待/暂停/完成/失败）带实时数量，翻页、批量暂停/继续
- **每任务操作按钮**：按状态显示——暂停/继续/详情/保存位置/获取链接/重试/删除记录
- **危险操作二次确认**：取消任务时可选「仅取消」或「取消并删除文件」，删除文件需再次确认；清理记录先显示条数再确认
- **失败可重试**：原始来源（URL/磁力/种子文件）持久化在数据库，失败任务一键「🔄 重试」；重复发送已下载过的内容会提示并提供「重新下载」
- **主页即仪表盘**：`/start` 一屏看全任务统计、实时网速、磁盘空间，带刷新按钮
- **设置即管理**：`/settings` 集中了限速调整（预设值即点即生效）、白名单管理、GoFile 开关、rclone 开关、远程重启服务

**下载与后处理**

- 大文件支持：自托管 telegram-bot-api（`--local` 模式），Bot 文件上限从 20MB 提升到 2GB
- 按类型自动归类保存（video/audio/photo/archive/other 子目录）
- 重复下载检测（URL 哈希 / Telegram file_unique_id 去重）
- 磁盘空间预检（按文件大小 ×1.2 + 1GiB 最低水位）
- **GoFile 管线**（可选）：下载完成 → 压缩（目录必压，单文件可选）→ 上传 gofile.io → 可选删除本地文件，全程后台执行不阻塞其他任务，链接回写任务卡片；未配置 token 时自动创建游客账号
- **rclone 上传**（可选）：接入 aria2 `on-download-complete` 钩子的网盘上传

**权限**

- 白名单：`.env` 种子名单（`ALLOWED_USER_IDS`）+ SQLite 动态名单（Telegram/Web 后台均可增删，即时生效，无需重启）

## 机器人命令

| 命令 | 作用 |
|---|---|
| `/start` | 主菜单（状态总览仪表盘） |
| `/list` | 任务列表（tab 筛选 + 翻页） |
| `/find 关键词` | 按文件名模糊搜索历史任务 |
| `/stats` | 下载统计（默认最近 7 天，可切换周期） |
| `/settings` | 设置与管理（限速/同时下载数/单文件上限/下载目录/自动清理/自动发送/白名单/GoFile/rclone/重启/服务器状态） |
| `/limit 2M` | 全局限速（`/limit 0` 取消）；设置页里也有预设按钮，任务卡片上还有单任务限速 |
| `/pause /resume /cancel <任务id>` | 命令方式操作任务（按钮已覆盖，保留兜底） |
| `/adduser <ID> [备注]` / `/removeuser <ID>` | 白名单增删（设置页里也可操作） |
| `/addnode 名称 rpc地址 密钥 [目录]` | 注册额外的 aria2 节点（仅管理员，见下方多节点说明） |
| `/admin` | `/settings` 的别名（历史遗留） |

发送多行链接（一行一个 URL/磁力）会自动识别成批量任务，生成一张汇总确认卡片，"▶️ 全部开始"一键添加。

## 多节点（一个 bot 控制多个 aria2 实例）

默认单节点（`.env` 里的 `ARIA2_RPC` 即内置的"本机"节点），行为与旧版完全一致。管理员用
`/addnode 群晖 http://192.168.1.5:6800/jsonrpc 密钥 /volume1/downloads` 注册额外节点后：

- 主菜单出现 **🖥 节点** 切换行，每个用户各自选择"当前节点"，新任务落到自己选中的节点；
- 发链接后的确认卡片上显示目标节点，也可以卡片上临时切换（不影响全局偏好）；
- 所有节点的任务统一轮询和展示（列表/卡片带 📍 节点标注），暂停/继续/取消/限速/文件选择跨节点可用；
- 设置 → **🌐 节点管理**（管理员）可停用/删除节点；`/addnode` 处理完会自动删除你发的那条含密钥的消息；
- 节点断线只影响它自己（任务不会被误标失败），恢复后自动继续。

注意：**发送到 TG / GoFile 上传 / 磁盘检查 / Telegram 文件转存**依赖 bot 本机的文件系统，
只对"本机"节点生效；远程节点的任务卡片上不会出现这些按钮，Telegram 文件转存固定走本机节点。
设置菜单里的全局限速/同时下载数也只作用于本机节点。

## 一键安装

```bash
git clone https://github.com/cuijianzhuang/tg-aria2-bot.git
cd tg-aria2-bot
sudo ./install.sh
```

不带参数运行会交互式询问部署方式和凭据；也可以全部用参数一次性跑完，方便无人值守部署：

```bash
sudo ./install.sh \
  --mode docker \
  --token 123456:ABC-xxx \
  --api-id 12345 \
  --api-hash 0123456789abcdef0123456789abcdef \
  --allowed-ids 111111,222222
```

`--mode` 支持 `docker` 或 `bare`，其余参数(`--token` `--api-id` `--api-hash` `--allowed-ids` `--download-dir`)缺省会交互式询问。
`API_ID` / `API_HASH` 在 https://my.telegram.org 申请。
加 `--with-rclone` 可选安装 rclone（网盘上传，默认不装，见下方"可选：rclone"一节）。
Web 管理后台默认启用，`--admin-password <PW>` 指定密码，不指定则自动生成并在安装结束时打印一次；`--no-web` 完全跳过（见下方"Web 管理后台"一节）。

## 两种部署方式的差异

| | docker | bare |
|---|---|---|
| aria2 | 容器 `p3terx/aria2-pro` | 宿主机，`aria2.sh` 一键安装（P3TERX 完美配置） |
| telegram-bot-api | 容器 `aiogram/telegram-bot-api` | 默认仍用一个独立容器（混合模式）；`--build-botapi-from-source` 可从源码编译成宿主机二进制，彻底摆脱 Docker |
| bot 进程 | 容器 | Python venv + systemd 服务 |
| 适用场景 | 全新机器、喜欢容器化管理 | 已有 aria2.sh 环境、不想装 Docker、资源受限的小机器 |

### docker 模式

```bash
sudo ./install.sh --mode docker ...
```

内部执行 `scripts/install_docker.sh`：检测/安装 Docker + compose 插件，`docker compose up -d --build`。

常用命令：
```bash
docker compose logs -f bot       # 机器人日志
docker compose logs -f aria2     # aria2 / 钩子脚本日志
docker compose restart bot
docker compose down
```

**从旧版本升级**：`bot`/`web` 容器现在以非 root 用户（UID/GID 1000，跟 `aria2` 容器的 `PUID`/`PGID` 一致）运行，权限最小化。全新安装（`install.sh`）会自动把 `downloads/`、`data/`、`aria2-config/`、`.env` 的属主设成 1000:1000，不用手动处理；如果是从更早的、容器内以 root 运行的版本升级上来（这几个文件/目录当初是用 root 创建的），容器会因为 `Permission denied` 起不来或设置回写失败，先在宿主机上执行一次：

```bash
sudo chown -R 1000:1000 downloads data aria2-config .env
```

（也可以直接重新跑一遍 `sudo ./install.sh --mode docker ...`，脚本本身现在会做这一步。）

### bare 模式

```bash
sudo ./install.sh --mode bare ...
```

内部执行 `scripts/install_bare.sh`：

1. 用官方 [`aria2.sh`](https://github.com/P3TERX/aria2.sh) 在宿主机安装 aria2 + 完美配置（含 tracker 自动更新、下载完成/停止钩子），配置在 `/root/.aria2c/`。
   脚本本体逐字复刻在 [`vendor/aria2.sh/aria2.sh`](vendor/aria2.sh/aria2.sh)（离线可用、可审计，不受上游后续改动影响；仓库里没有才会回退联网拉取）。
   `aria2.sh` 是纯交互式菜单脚本（没有非交互 flag），我们的脚本用 `printf '1\n' | bash aria2.sh` 自动选中菜单里的"1. 安装 Aria2"。
   注意：`aria2.sh` 内部安装 aria2 二进制和完美配置这两步本身仍然需要联网（从 GitHub Releases / CDN 镜像下载），只有"安装脚本本体"这一层是离线的。
   安装流程本身全自动（装依赖 → 下载静态二进制 → 下载完美配置 → 注册 init.d 服务），**但它会自动修改并持久化 iptables 规则**，放行 RPC(6800)/BT(51413)/DHT(51413) 端口
   （Debian 写 `/etc/iptables.up.rules` + `if-pre-up.d` 钩子，CentOS 用 `service iptables save`）。如果你用 ufw/firewalld/云安全组管理防火墙，装完后检查一下有没有冲突或冗余规则。
   RPC 密钥由 aria2.sh 安装时自动随机生成，写在 `/root/.aria2c/aria2.conf` 里，我们的脚本会读出来同步进 `.env`。
2. telegram-bot-api：
   - 默认：仅用 `docker run` 起一个独立容器（不依赖 compose，其余服务都是裸机），端口只绑定 `127.0.0.1:8081`。
   - 加 `--build-botapi-from-source`：从源码编译 tdlib + telegram-bot-api 装到 `/usr/local/bin`，走 systemd 管理，彻底不用 Docker（耗时 20-40 分钟，需要 2GB+ 内存）：
     ```bash
     sudo ./install.sh --mode bare --token ... --api-id ... --api-hash ... --allowed-ids ...
     # 若已生成 .env，可单独重跑：
     sudo bash scripts/install_bare.sh --build-botapi-from-source
     ```
3. 机器人：创建 `.venv`，装依赖，注册为 `tg-aria2-bot.service`。

常用命令：
```bash
systemctl status tg-aria2-bot
journalctl -u tg-aria2-bot -f
systemctl status aria2
```

## Web 管理后台

默认启用两个 Web 界面（都只监听 `127.0.0.1`，不映射公网端口；远程访问用 SSH 隧道 `ssh -L 8080:localhost:8080 -L 6880:localhost:6880 user@server`，或自己套一层带 TLS+认证的反向代理）：

| | 地址 | 作用 | 认证 |
|---|---|---|---|
| **自建管理后台** | http://127.0.0.1:8080 | 机器人自己的业务数据：任务列表（暂停/恢复/取消）、全局限速、白名单用户管理、GoFile/rclone 配置、磁盘用量、修改密码、远程重启 | 单一管理密码（`ADMIN_PASSWORD`），登录后签发 HMAC 签名的 cookie，7 天有效；改密码自动失效所有旧会话 |
| **AriaNg** | http://127.0.0.1:6880 | 现成的 aria2 可视化面板：完整任务详情、BT 分享率、连接数等 aria2 原生信息 | 无内建认证，靠只监听 127.0.0.1 这一层挡住外部访问；首次打开在设置页填 RPC 地址 `http://127.0.0.1:6800/jsonrpc` 和密钥（`.env` 里的 `ARIA2_SECRET`），之后记在浏览器 localStorage |

两者分工不重叠：AriaNg 只管 aria2 层面的任务，看不到 Telegram 用户、白名单这些机器人自己的数据；自建后台反过来不重复 AriaNg 已经做得很好的 aria2 任务详情展示。

### 白名单管理

`ALLOWED_USER_IDS`（`.env`）是**种子名单**，改它需要重新跑一次安装或手动改 `.env` 重启；Telegram 设置页或自建后台里新增/删除的用户存在 SQLite 的 `allowed_users` 表里，**不需要重启就能生效**，机器人下次收到消息时直接查库。两者是并集关系——种子名单里的用户在后台看得到但删不掉（会提示去改 `.env`），后台加的用户可以随时删。

如果 `.env` 里 `ALLOWED_USER_IDS` 留空，白名单机制整体关闭（机器人对所有人开放），这时候后台加人也不会有实际效果。

### 关闭 Web 管理后台

```bash
sudo ./install.sh --no-web ...
```

docker 模式下 `web`/`ariang` 两个服务标了 compose profile `web`，不传 `--profile web` 就不会启动，`docker compose ps` 也看不到它们，没有额外的镜像/端口占用。bare 模式下直接不注册对应的 systemd 服务。之后想重新开启，`ADMIN_PASSWORD` 不为空时重新跑一次 `docker compose --profile web up -d`（docker）或重新跑 `install_bare.sh`（bare）即可，不用整个重装。

## 可选：GoFile 自动上传

下载完成后自动 压缩 → 上传 [gofile.io](https://gofile.io) → （可选）删除本地文件。在 Telegram 设置页（`/settings` → ☁️ GoFile）或 Web 后台开关，`.env` 对应项：

```ini
GOFILE_ENABLED=true          # 总开关
GOFILE_TOKEN=                # 留空自动创建游客账号；填自己的 token 则归档到自己账号下
GOFILE_COMPRESS=true         # 上传前 zip（多文件目录必压缩，与此开关无关）
GOFILE_DELETE_LOCAL=false    # 上传成功后删除本地文件（确认上传成功才删）
```

管线由 bot 进程执行（非 aria2 钩子），在后台任务中运行，不阻塞其他任务的进度更新；上传链接回写到任务卡片和数据库。Telegram 设置页的开关**即时生效**（同进程改内存 + 写回 `.env`）；直接手改 `.env` 则需要重启 bot。

## 可选：rclone（网盘自动上传，默认不装）

`p3terx/aria2-pro` 镜像**本身不带 rclone**（已翻过其 Dockerfile 和 rootfs，确认没有）；bare 模式下 `aria2.sh` 装的完美配置里虽然有 `rclone.env` 模板，但 rclone 二进制同样得自己装。装了也不会自动生效——因为 `upload.sh` 默认没接入 `on-download-complete` 钩子。

```bash
sudo ./install.sh --mode docker --with-rclone ...   # 或 --mode bare --with-rclone ...
```

两种模式都通过 [`scripts/install_rclone.sh`](scripts/install_rclone.sh) 把 rclone **装在宿主机**（跑官方 `curl https://rclone.org/install.sh | bash`，二进制落在 `/usr/bin/rclone`），而不是塞进某个容器镜像里：
- **docker 模式**：装完宿主机的 rclone 后，生成 `docker-compose.override.yml`，把宿主机的 `/usr/bin/rclone` 只读挂载进 `aria2` 容器同一路径，不用重新 build 镜像。rclone 官方 Linux 二进制是纯静态链接的 Go 可执行文件（已用 `file`/`ldd` 验证，无 glibc 依赖），挂进 `p3terx/aria2-pro` 用的 Alpine(musl) 容器不会有兼容性问题。升级 rclone 只需要在宿主机重新跑一次安装脚本，容器里立刻就是新版本。
- **bare 模式**：aria2 本来就跑在宿主机，rclone 装在宿主机后直接就能被 `upload.sh` 调用，无需额外处理。

两种模式装完都只是有了宿主机上的 `rclone` 命令，**配置网盘 remote 需要交互式 OAuth 授权，没法自动化**：
```bash
docker compose exec -it aria2 rclone config     # docker 模式（容器内看到的是同一份宿主机二进制/配置）
rclone config                                    # bare 模式
```
配好 remote 后，在 Telegram 设置页（`/settings` → 📁 rclone）或 Web 后台一键切换 `on-download-complete` 钩子指向 `upload.sh`，重启 aria2 生效；钩子路径随部署模式不同，由 `.env` 的 `ARIA2_CLEAN_HOOK` / `ARIA2_UPLOAD_HOOK` 配置。

## 部署后必查

- `.env` 中的 `ARIA2_SECRET`：脚本会自动生成或从 aria2.sh 已生成的配置里同步，不要用默认值。
- `.env` 中的 `ADMIN_PASSWORD`：安装时没指定会自动生成并只打印一次，确认已经记下来了；忘记了就直接改 `.env` 重启 `tg-aria2-web`（bare）或 `docker compose restart web`（docker）。
- 两种模式下 `telegram-bot-api`、`aria2`、web 管理后台、AriaNg 的端口都只监听内网或 `127.0.0.1`，不要映射到公网；要远程访问用 SSH 隧道或自己套反向代理+TLS。
- `move.sh` / `upload.sh`：这两个脚本**默认没有接入任何 aria2 钩子**（`aria2.conf` 里 `on-download-complete` 只指向 `clean.sh`，`clean.sh` 只做 `.aria2`/`.torrent`/空目录清理，不会移动或上传文件），不需要手动关闭。要启用网盘自动上传见上面 rclone 一节。

## 开发与运维

### 更新部署

```bash
./deploy.sh                # 同步 bot/ + requirements.txt → 服务器端编译/导入校验 → 重启服务
./deploy.sh --no-restart   # 只同步和校验，不重启
```

服务器地址/SSH key/目录/服务名写在脚本开头，按自己的环境改。脚本**永远不会覆盖服务器上的 `.env`**（真实密钥只存在于服务器）。

### 自动部署（GitHub Actions）

推到 `master` 会自动部署：[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) 通过 SSH 登录服务器执行
`git fetch && git reset --hard origin/master` → 编译/导入/单测校验 → `systemctl restart tg-aria2-bot`，任何一步失败整个 job 失败，不会重启到半吊子状态。

前提是服务器上的 `/root/tg-aria2-bot` 是这个仓库的 git clone（不是 `deploy.sh` 那种文件同步），且以下三个仓库 Secret 已配置（`Settings → Secrets and variables → Actions`）：

| Secret | 说明 |
|---|---|
| `DEPLOY_SSH_KEY` | 部署专用私钥（不要用你日常登录用的私钥） |
| `DEPLOY_HOST` | 服务器地址 |
| `DEPLOY_USER` | SSH 用户名 |

`git reset --hard` 只会动 git 跟踪的文件——`.env`、`data/`、`.venv/`、`aria2-bare/`（bare 模式下真实运行的 aria2 配置/会话数据）、`downloads/`（实际下载内容）这些运行时目录都在 `.gitignore` 里，不会被覆盖或删除。想临时关掉自动部署，去 Actions 页面禁用这个 workflow，或者删掉 `.github/workflows/deploy.yml`。

### 测试

纯函数渲染层（卡片文案、HTML 转义、进度条、键盘结构）有单元测试，在有有效 `.env` 的机器上跑：

```bash
.venv/bin/python -m unittest discover tests -v
```

### 数据库

SQLite（默认 `data/tasks.db`，`aiosqlite` 异步访问）。schema 在 [`bot/db/models.py`](bot/db/models.py)，新增列走 `MIGRATIONS` 列表（幂等的 `ALTER TABLE`，启动时自动执行，重复跑会跳过）。

## 目录结构

```
tg-aria2-bot/
├── install.sh                  # 一键安装入口
├── deploy.sh                   # 更新部署（同步 → 校验 → 重启）
├── scripts/
│   ├── install_docker.sh
│   ├── install_bare.sh
│   └── install_rclone.sh         # 两种模式共用：把 rclone 装在宿主机（--with-rclone 时调用）
├── systemd/                    # bare 模式用的 unit 模板（含 tg-aria2-web、tg-ariang）
├── docker-compose.yml
├── Dockerfile
├── Dockerfile.ariang            # nginx + 官方 AriaNg 静态构建产物（docker 模式）
├── requirements.txt
├── .env.example
├── tests/                      # 渲染层单元测试
├── aria2-config/                # 预置的 P3TERX/aria2.conf 文件，路径已适配本项目（docker 模式用）
│   ├── aria2.conf                # dir=/downloads, rpc-secret 由 install.sh 自动写入
│   ├── script.conf
│   ├── rclone.env                 # 默认未接入钩子，见"可选：rclone"一节
│   └── script/upload.sh           # 必须放在这里，见下方说明
├── vendor/                      # 上游文件的逐字 1:1 复刻，路径未做任何改动，仅供离线安装/审计对照
│   ├── aria2.sh/aria2.sh          # https://github.com/P3TERX/aria2.sh
│   └── aria2.conf/                # https://github.com/P3TERX/aria2.conf（原始 /root/Download、/root/.aria2 路径）
└── bot/                        # 机器人源码
    ├── main.py                   # 入口：Dispatcher、命令菜单、启动对账
    ├── config.py                 # pydantic-settings，全部配置项及注释
    ├── handlers/                 # aiogram 路由
    │   ├── commands.py             # /start /list /pause 等命令
    │   ├── callbacks.py            # 全部按钮回调（导航/列表/任务操作/设置）
    │   ├── admin.py                # 设置页管理功能（白名单/GoFile/rclone/重启）
    │   ├── links.py                # URL / 磁力 / .torrent 消息
    │   └── media.py                # 转发的媒体文件
    ├── core/
    │   ├── aria2_client.py         # aria2p 的异步封装
    │   ├── task_manager.py         # 轮询进度、节流编辑、GoFile 管线
    │   ├── cards.py                # 所有卡片文案渲染
    │   ├── keyboards.py            # 所有 InlineKeyboard 构建
    │   ├── list_view.py            # tab 式任务列表渲染
    │   ├── pending_tasks.py        # 待确认任务（内存 TTL 30 分钟）
    │   ├── gofile.py               # gofile.io API（含游客 token 自动创建）
    │   ├── compress.py             # zip 压缩/删除
    │   ├── storage.py              # 目录归类、磁盘检查、URL 哈希
    │   ├── telegram_files.py       # 本地 bot-api 绝对路径 → file:// URI 适配
    │   └── conf_editor.py          # .env / aria2.conf / script.conf 读写
    ├── db/                       # SQLite：schema + 迁移 + 仓储
    ├── middlewares/auth.py       # 白名单校验（env 种子 + DB 动态名单）
    └── web/                      # 自建管理后台：FastAPI + 纯静态 HTML/JS 前端，无构建步骤
        ├── app.py
        ├── auth.py                 # 单密码 + HMAC 签名 cookie，不依赖数据库存 session
        └── static/                 # index.html / app.js / style.css
```

`aria2-config/` 里的文件取自 https://github.com/P3TERX/aria2.conf (MIT License)，调整了路径（`/root/Download` → `/downloads`，`/root/.aria2` → `/config`）以适配本项目的 docker 部署，容器首次启动直接使用这份配置，不需要联网去 GitHub 拉取。
`vendor/` 里的文件是**未经任何修改**的原始副本（路径仍是上游默认的 `/root/...`），存在这里只是为了离线安装（`scripts/install_bare.sh` 会优先用 `vendor/aria2.sh/aria2.sh`）和审计对照，不会被本项目直接引用运行。

**重要**：`aria2-config/` 里不再放 `core`/`clean.sh`/`delete.sh`/`tracker.sh`——实测 `p3terx/aria2-pro` 镜像首次启动会用它自己的这几个文件覆盖到容器内的 `/config/script/`（它的 `core` 把 `ARIA2_CONF_DIR` 写死为 `/config`，不依赖脚本物理路径，比我们原来 vendor 的 `$(dirname $0)` 写法更健壮，所以直接用镜像自带的更省心），放在仓库顶层也会被起容器时清掉，纯属误导。`upload.sh` 是镜像不自带的额外功能，必须放在 `aria2-config/script/upload.sh`（对应容器内 `/config/script/upload.sh`）才能和镜像自己的 `core` 配套工作；`aria2.conf` 里的 `on-download-complete`/`on-download-stop` 也相应指向 `/config/script/*.sh`，不能是 `/config/` 顶层。

## 致谢

- [P3TERX/aria2.conf](https://github.com/P3TERX/aria2.conf) / [P3TERX/aria2.sh](https://github.com/P3TERX/aria2.sh) — aria2 完美配置与安装脚本（MIT）
- [aiogram](https://github.com/aiogram/aiogram) · [aria2p](https://github.com/pawamoy/aria2p) · [AriaNg](https://github.com/mayswind/AriaNg)
