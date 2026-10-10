# tg-aria2-bot

Telegram 下载机器人：给机器人发送 **HTTP(S) 链接 / 磁力链接 / 裸 BT infohash / .torrent 文件 / 转发媒体消息**，
由服务器上的 aria2（[P3TERX 完美配置](https://github.com/P3TERX/aria2.conf)）执行下载，
机器人以卡片形式实时回报进度，并可选压缩上传 GoFile 网盘。附带 Web 管理后台 + AriaNg 面板。

## 功能特性

**Telegram 交互（卡片式，全按钮操作）**

- **确认后下载**：发送链接/种子后先出确认卡片（文件名/大小/保存位置），点「▶️ 开始下载」才真正入队，防误触
- **实时任务卡片**：下载中只占 4 行——名称·大小 / 进度条 / 已下载·速度·剩余时间 / 连接数·上传速度·时间；种子/磁力任务显示内容的真实名字而不是 `.torrent` 文件名；保存路径只在完成/失败的卡片上出现。**看的时候快、不看的时候慢**：刚添加的任务、刚点过卡片按钮后的 2 分钟内每 2 秒刷新一次（想看实时进度就点一下「🔄 刷新」），之后降到 30 秒一次；同一聊天里同时有多张卡片会自动再放慢，避开 Telegram 的编辑频率限制；打开限速/取消确认等子菜单时暂停自动刷新，不会把菜单冲掉（`PROGRESS_*` 可调）
- **任务列表**：单页浏览，顶部分段式筛选 tab（全部/下载中/等待/暂停/完成/失败）带实时数量，每行带状态图标；翻页、刷新，「下载中」tab 一键全部暂停、「已暂停」tab 一键全部继续。从列表点开任务后，「⬅️ 返回列表」回到原来的 tab 和页码（中途进限速、选择文件、取消确认等子菜单也不会丢）
- **每任务操作按钮**：按状态显示——下载中：暂停/刷新/选择文件（仅多文件任务）/单任务限速/取消；已完成：打开 GoFile 链接（直接跳转）/保存位置/发送到 TG/删除；失败：重试/删除；已取消：重新下载/删除记录
- **危险操作二次确认**：取消任务时可选「仅取消」或「取消并删除文件」；删除已完成任务时可选「仅删除记录」或「删除记录和文件」，删文件都要再确认一次（只会删下载目录里面的东西）；清理记录先显示条数再确认
- **失败可重试**：原始来源（URL/磁力/种子文件）持久化在数据库，失败任务一键「🔄 重试」；重复发送已下载过的内容会提示并提供「重新下载」
- **主页即仪表盘**：`/start` 一屏看全任务统计、实时网速、磁盘空间；按钮直达下载中/任务列表/统计，有失败任务时多一个「⚠️ 失败 N」入口
- **设置即管理**（仅管理员）：`/settings` 集中了限速调整（预设值即点即生效，当前值高亮）、白名单管理、GoFile 开关、rclone 开关、远程重启服务；普通用户的主菜单不显示「设置」按钮

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

发送多行链接（一行一个 URL/磁力/裸 infohash）会自动识别成批量任务，生成一张汇总确认卡片，"▶️ 全部开始"一键添加。

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
sudo ./tg-aria2.sh install
```

安装、升级、日常管理全部由一个脚本 [`tg-aria2.sh`](tg-aria2.sh) 完成（`./install.sh`、`./update.sh` 只是转发给它的兼容入口）。

不带参数运行会交互式询问部署方式和凭据；也可以全部用参数一次性跑完，方便无人值守部署：

```bash
sudo ./tg-aria2.sh install \
  --mode docker \
  --token 123456:ABC-xxx \
  --api-id 12345 \
  --api-hash 0123456789abcdef0123456789abcdef \
  --allowed-ids 111111,222222
```

`--mode` 支持 `docker` 或 `bare`，其余参数(`--token` `--api-id` `--api-hash` `--allowed-ids` `--download-dir`)缺省会交互式询问。
重复运行是安全的：已有 `.env` 时凭据/模式/密钥自动沿用（不用重新输入，`ARIA2_SECRET`、`ADMIN_PASSWORD` 不会被重新生成），
而且**只改脚本自己管理的那几个键**，你在设置菜单/Web 后台里改过的配置（同时下载数、GoFile、自动清理……）原样保留。
`API_ID` / `API_HASH` 在 https://my.telegram.org 申请。
加 `--with-rclone` 可选安装 rclone（网盘上传，默认不装，见下方"可选：rclone"一节）。
**安装中途失败也不用慌**：脚本会说明是哪一步失败的，按提示处理后重新运行 `sudo ./tg-aria2.sh install` 即可，已完成的步骤会自动跳过。
常见的坑都已经自动处理：Debian/Ubuntu 上缺 `python3-venv` 会自动安装；系统 Python 低于 3.11 会尝试装 `python3.11`；端口被占用会自动换；AriaNg 下载失败（访问不了 GitHub）只警告不中断。
只想单独装 / 修复 Web 管理后台：`sudo ./tg-aria2.sh web-install`。

Web 管理后台默认启用，`--admin-password <PW>` 指定密码，不指定则自动生成并在安装结束时打印一次；`--web-port <端口>` 指定端口（默认 8080，被占用时自动顺延到下一个空闲端口）；`--no-web` 完全跳过（见下方"Web 管理后台"一节）。

## 管理菜单

安装完成后会自动注册快捷命令 `tg-aria2`，日常运维不用记各种 docker / systemctl 命令：

```bash
sudo tg-aria2          # 或者在仓库目录里 sudo ./tg-aria2.sh
```

```
========== tg-aria2-bot 管理菜单 ==========
 部署模式: docker    版本: 0135c05（2026-10-07）
 bot ● 运行中   web ● 运行中   aria2 ● 运行中   telegram-bot-api ● 运行中   ariang ● 运行中
 下载目录: ./downloads  剩余 120G

 —— 运行 ——
   1. 服务状态
   2. 查看日志
   3. 重启服务
   4. 启动 / 停止全部服务
 —— 配置 ——
   5. 修改常用配置（白名单/管理员/密码/并发/代理/端口…）
   6. Web 后台访问信息
 —— 升级与备份 ——
   7. 检查更新
   8. 升级到最新版本
   9. 立即备份
  10. 从备份恢复（.env / 数据库）
  11. 回退到历史版本
 —— 安装 ——
  12. 安装 / 修复安装
  13. 安装 / 修复 Web 管理后台
  14. 安装快捷命令 tg-aria2
   0. 退出
```

- 自动识别 docker / bare 模式，同一套菜单操作两种部署（bare 混合模式下的 telegram-bot-api 独立容器也能管）
- 菜单顶部发现核心服务没装上（比如上次安装中途失败）会直接提示，选 12 修复
- **修改常用配置**：白名单、管理员、Web 后台密码（留空自动生成，改完旧登录会话全部失效）、同时下载数、磁盘告警、自动清理、代理、Web 端口监听地址；输入会校验格式，改完询问是否立即重启生效；也可以直接用编辑器打开 `.env`
- **从备份恢复**：从 `backups/` 里选一份，恢复 `.env` 和数据库（恢复前会先把当前状态再备份一次，防止误操作）
- **回退到历史版本**：列出最近 15 个版本选择，或输入任意提交号/标签；走 `tg-aria2.sh update --to`，同样有备份、健康检查和自动回滚

也可以带子命令直接运行，不进菜单：`tg-aria2 status`、`tg-aria2 logs aria2`、`tg-aria2 restart bot`、`tg-aria2 backup`、`tg-aria2 update`、`tg-aria2 web-install`……（`tg-aria2 --help` 查看全部）

## 升级

```bash
cd tg-aria2-bot
sudo tg-aria2 check        # 只看有没有新版本、有哪些更新，不做任何改动
sudo tg-aria2 update       # 升级（列出新提交，确认后执行）
sudo tg-aria2 update -y    # 不询问直接升级，适合放进 cron
```

升级自动识别部署模式（docker / bare），一条命令完成整个流程：

1. `git fetch` 并列出将要更新的提交；提示依赖、`.env.example`、`aria2-config/` 模板是否有变化
2. **备份**到 `backups/<时间戳>/`：`.env`、数据库（SQLite 在线备份，运行中也是一致的快照）、当前版本号，保留最近 10 份
3. 更新代码（fast-forward）。`aria2-config/` 里被运行时改写过的配置（RPC 密钥、rclone 钩子……）会先存起来、更新后原样放回——不会再因为"本地有改动"导致 `git pull` 失败，也不会被上游模板覆盖
4. 应用：
   - docker：`docker compose build` + `up -d`，顺带把 bind mount 目录属主对齐到容器用户 1000（从老的 root 容器版本升级时必需）；加 `--pull-images` 同时更新 telegram-bot-api / aria2 / Python 基础镜像
   - bare：requirements.txt 有变化才重装依赖 → **重启前**先做编译 + 导入校验 → systemd 单元模板有变化才同步（原文件先备份）→ 重启 bot / web
5. **健康检查**：观察 20 秒，服务挂掉或被 systemd/docker 反复拉起都算失败
6. 任何一步失败**自动回滚**到升级前的版本并重新拉起服务（数据库迁移只增不删，旧代码可以直接用新 schema）；`--no-rollback` 可以保留现场排查

其它选项：`--to <版本>`（回退/切换到指定提交或标签）、`--backup-only`（只备份）、`--force`（没有新提交也重新应用一遍）、`--reset`（本地分支和远端分叉时强制对齐远端）、`--branch NAME`。`tg-aria2 --help` 查看全部。
升级、回退、恢复在管理菜单里都有对应入口。

升级后如果提示 `.env.example` 有新配置项，按需用 `git diff <旧版本> <新版本> -- .env.example` 对照添加；不加也能正常运行（都有默认值），`.env` 里多出旧版本不认识的键也不会导致启动失败。

## 两种部署方式的差异

| | docker | bare |
|---|---|---|
| aria2 | 容器 `p3terx/aria2-pro` | 宿主机，`aria2.sh` 一键安装（P3TERX 完美配置） |
| telegram-bot-api | 容器 `aiogram/telegram-bot-api` | 默认仍用一个独立容器（混合模式）；`--build-botapi-from-source` 可从源码编译成宿主机二进制，彻底摆脱 Docker |
| bot 进程 | 容器 | Python venv + systemd 服务 |
| 适用场景 | 全新机器、喜欢容器化管理 | 已有 aria2.sh 环境、不想装 Docker、资源受限的小机器 |

### docker 模式

```bash
sudo ./tg-aria2.sh install --mode docker ...
```

检测/安装 Docker + compose 插件，检查 Web 后台端口，`docker compose up -d --build`。

常用命令：
```bash
docker compose logs -f bot       # 机器人日志
docker compose logs -f aria2     # aria2 / 钩子脚本日志
docker compose restart bot
docker compose down
```

是否启动 Web 后台由 `.env` 里的 `COMPOSE_PROFILES=web` 决定，手敲 `docker compose up -d` 也会带上 `web`/`ariang`，不用每次加 `--profile web`。
下载目录、Web 端口监听地址也在 `.env` 里配置（`HOST_DOWNLOAD_DIR`、`WEB_BIND`），见 [`.env.example`](.env.example) 末尾。
所有容器日志都做了轮转（单文件 10MB × 3），长期运行不会撑满磁盘。

`bot`/`web` 容器以非 root 用户（UID/GID 1000，跟 `aria2` 容器的 `PUID`/`PGID` 一致）运行。安装和升级都会自动把
`data/`、`aria2-config/`、`.env` 和下载目录的属主对齐成 1000:1000；从很早的 root 容器版本升级上来，直接跑 `sudo tg-aria2 update` 即可。

### bare 模式

```bash
sudo ./tg-aria2.sh install --mode bare ...
```

依次执行（每一步失败都会说明是哪一步，重跑自动跳过已完成的）：

1. 用官方 [`aria2.sh`](https://github.com/P3TERX/aria2.sh) 在宿主机安装 aria2 + 完美配置（含 tracker 自动更新、下载完成/停止钩子），配置在 `/root/.aria2c/`。
   脚本本体逐字复刻在 [`vendor/aria2.sh/aria2.sh`](vendor/aria2.sh/aria2.sh)（离线可用、可审计，不受上游后续改动影响；仓库里没有才会回退联网拉取）。
   `aria2.sh` 是纯交互式菜单脚本（没有非交互 flag），我们的脚本用 `printf '1\n' | bash aria2.sh` 自动选中菜单里的"1. 安装 Aria2"。
   注意：`aria2.sh` 内部安装 aria2 二进制和完美配置这两步本身仍然需要联网（从 GitHub Releases / CDN 镜像下载），只有"安装脚本本体"这一层是离线的。
   安装流程本身全自动（装依赖 → 下载静态二进制 → 下载完美配置 → 注册 init.d 服务），**但它会自动修改并持久化 iptables 规则**，放行 RPC(6800)/BT(51413)/DHT(51413) 端口
   （Debian 写 `/etc/iptables.up.rules` + `if-pre-up.d` 钩子，CentOS 用 `service iptables save`）。如果你用 ufw/firewalld/云安全组管理防火墙，装完后检查一下有没有冲突或冗余规则。
   RPC 密钥由 aria2.sh 安装时自动随机生成，写在 `/root/.aria2c/aria2.conf` 里，我们的脚本会读出来同步进 `.env`。
2. telegram-bot-api：
   - 默认：仅用 `docker run` 起一个独立容器（不依赖 compose，其余服务都是裸机），端口只绑定 `127.0.0.1:8081`。
     - 8081 上**已经是一个 telegram-bot-api**（比如之前源码编译装过、或别的名字的容器）：直接复用，不再另起
     - 8081 被**其它程序**占用：自动改用 8082–8099 里第一个空闲端口并写进 `.env` 的 `BOT_API_URL`，安装脚本会提示是谁占着 8081
     - 8081 跟 Web 管理后台的端口（`WEB_PORT`）相同，或被本项目自带的 Web 后台占着：同样让 telegram-bot-api 换端口，Web 后台端口保持不变
     - 想手动指定端口：`sudo BOT_API_PORT=9081 ./tg-aria2.sh install --mode bare ...`
   - 加 `--build-botapi-from-source`：从源码编译 tdlib + telegram-bot-api 装到 `/usr/local/bin`，走 systemd 管理，彻底不用 Docker（耗时 20-40 分钟，需要 2GB+ 内存）：
     ```bash
     sudo ./tg-aria2.sh install --mode bare --botapi-from-source --token ... --api-id ... --api-hash ... --allowed-ids ...
     ```
3. 机器人：找 Python ≥ 3.11（没有就尝试装 `python3.11`），创建 `.venv`（缺 venv 模块自动装 `python3.x-venv`），装依赖，先确认代码能正常导入，再注册为 `tg-aria2-bot.service`。
4. Web 管理后台 `tg-aria2-web.service` + AriaNg（`--no-web` 时跳过；AriaNg 下载失败只警告）。

常用命令：
```bash
systemctl status tg-aria2-bot
journalctl -u tg-aria2-bot -f
systemctl status aria2
```

## Web 管理后台

默认启用两个 Web 界面。监听范围随部署模式不同：

- **bare 模式**：只监听 `127.0.0.1`
- **docker 模式**：默认 `0.0.0.0`（对公网开放，**明文 HTTP**）。建议在 `.env` 里设 `WEB_BIND=127.0.0.1` 后 `docker compose up -d`，改为只监听本机

端口默认 8080，由 `.env` 的 `WEB_PORT` 决定（docker 模式下是宿主机端口）。装好后要改，用管理菜单 `sudo tg-aria2` →「修改常用配置」→「Web 后台端口」，会检查端口是否被占用、是否跟 telegram-bot-api 冲突，并自动更新 systemd 单元/重建容器。

只监听本机时，远程访问用 SSH 隧道 `ssh -L 8080:localhost:8080 -L 6880:localhost:6880 user@server`，或自己套一层带 TLS 的反向代理（Caddy 两行配置即可自动签证书）：

| | 地址 | 作用 | 认证 |
|---|---|---|---|
| **自建管理后台** | http://127.0.0.1:8080 | 机器人自己的业务数据：任务列表（暂停/恢复/取消）、全局限速、白名单用户管理、GoFile/rclone 配置、磁盘用量、修改密码、远程重启 | 单一管理密码（`ADMIN_PASSWORD`），登录后签发 HMAC 签名的 cookie，7 天有效；改密码自动失效所有旧会话 |
| **AriaNg** | http://127.0.0.1:6880 | 现成的 aria2 可视化面板：完整任务详情、BT 分享率、连接数等 aria2 原生信息 | 无内建认证（aria2 RPC 密钥本身是一道门槛）；首次打开在设置页填 RPC 地址 `http://127.0.0.1:6800/jsonrpc` 和密钥（`.env` 里的 `ARIA2_SECRET`），之后记在浏览器 localStorage |

两者分工不重叠：AriaNg 只管 aria2 层面的任务，看不到 Telegram 用户、白名单这些机器人自己的数据；自建后台反过来不重复 AriaNg 已经做得很好的 aria2 任务详情展示。

### 白名单管理

`ALLOWED_USER_IDS`（`.env`）是**种子名单**，改它需要重新跑一次安装或手动改 `.env` 重启；Telegram 设置页或自建后台里新增/删除的用户存在 SQLite 的 `allowed_users` 表里，**不需要重启就能生效**，机器人下次收到消息时直接查库。两者是并集关系——种子名单里的用户在后台看得到但删不掉（会提示去改 `.env`），后台加的用户可以随时删。

如果 `.env` 里 `ALLOWED_USER_IDS` 留空，白名单机制整体关闭（机器人对所有人开放），这时候后台加人也不会有实际效果。

### 关闭 Web 管理后台

```bash
sudo ./tg-aria2.sh install --no-web ...
```

docker 模式下 `web`/`ariang` 两个服务标了 compose profile `web`，`--no-web` 会把 `.env` 里的 `COMPOSE_PROFILES` 置空，它们就不会启动，没有额外的镜像/端口占用。bare 模式下直接不注册对应的 systemd 服务。之后想重新开启，运行 `sudo ./tg-aria2.sh web-install`（两种模式通用，密码为空时会自动生成），不用整个重装。

## 可选：GoFile 自动上传

下载完成后自动 压缩 → 上传 [gofile.io](https://gofile.io) → （可选）删除本地文件。在 Telegram 设置页（`/settings` → ☁️ GoFile）或 Web 后台开关，`.env` 对应项：

```ini
GOFILE_ENABLED=true          # 总开关
GOFILE_TOKEN=                # 留空自动创建游客账号；填自己的 token 则归档到自己账号下
GOFILE_COMPRESS=true         # 上传前 zip（多文件目录必压缩，与此开关无关）
GOFILE_DELETE_LOCAL=false    # 上传成功后删除本地文件（确认上传成功才删）
```

管线由 bot 进程执行（非 aria2 钩子），在后台任务中运行，不阻塞其他任务的进度更新；上传链接回写到任务卡片和数据库。Telegram 设置页的开关**即时生效**（同进程改内存 + 写回 `.env`），重启后也会保持；直接手改 `.env` 则需要重启 bot（管理菜单「重启服务」）。

## 可选：rclone（网盘自动上传，默认不装）

`p3terx/aria2-pro` 镜像**本身不带 rclone**（已翻过其 Dockerfile 和 rootfs，确认没有）；bare 模式下 `aria2.sh` 装的完美配置里虽然有 `rclone.env` 模板，但 rclone 二进制同样得自己装。装了也不会自动生效——因为 `upload.sh` 默认没接入 `on-download-complete` 钩子。

```bash
sudo ./tg-aria2.sh install --mode docker --with-rclone ...   # 或 --mode bare --with-rclone ...
```

两种模式都把 rclone **装在宿主机**（跑官方 `curl https://rclone.org/install.sh | bash`，二进制落在 `/usr/bin/rclone`），而不是塞进某个容器镜像里：
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
- `telegram-bot-api`、`aria2` RPC 端口只在 docker 内网 / `127.0.0.1` 上，不要映射到公网。docker 模式下 web 后台(8080)/AriaNg(6880) 默认对公网开放，确认这是你想要的；否则设 `WEB_BIND=127.0.0.1`（见上方"Web 管理后台"）。
- `move.sh` / `upload.sh`：这两个脚本**默认没有接入任何 aria2 钩子**（`aria2.conf` 里 `on-download-complete` 只指向 `clean.sh`，`clean.sh` 只做 `.aria2`/`.torrent`/空目录清理，不会移动或上传文件），不需要手动关闭。要启用网盘自动上传见上面 rclone 一节。

## 开发与运维

### 三种更新方式

| 方式 | 适用场景 | 做了什么 |
|---|---|---|
| `sudo tg-aria2 update`（服务器上） | **日常升级，推荐** | 见上方"升级"一节：备份 → 拉代码 → 装依赖/重建 → 健康检查 → 失败自动回滚 |
| 推送到 `master`（GitHub Actions） | 自己维护 fork、想推完代码自动上线 | CI 全部通过后 SSH 到服务器执行 `tg-aria2.sh update --reset`，同样有备份和自动回滚 |
| `./deploy.sh`（开发机上） | 开发调试，把**未提交**的本地改动直接推上去验证 | tar 同步 `bot/` + `requirements.txt` → 装依赖 → 编译/导入校验 → 重启 bot/web。没有回滚，不要用于正式发布 |

`deploy.sh` 的服务器地址/SSH key/目录可以用环境变量 `DEPLOY_HOST` / `DEPLOY_KEY` / `DEPLOY_DIR` 覆盖。三种方式都**不会覆盖服务器上的 `.env`**。

### 自动部署（GitHub Actions）

- [`test.yml`](.github/workflows/test.yml)：PR 和非 master 分支的推送触发——ruff、编译 + 导入检查、全部单元测试（Python 3.11 和 3.14 两个版本，分别对应 bare 模式最低版本和 docker 镜像版本）、shellcheck 检查安装/升级脚本
- [`deploy.yml`](.github/workflows/deploy.yml)：推到 `master`（或在 Actions 页面手动触发）时先复用 `test.yml` 跑一遍，全部通过后 SSH 登录服务器执行 `tg-aria2.sh update --reset --yes`。用的是 `origin/master` 上最新的 `tg-aria2.sh`，所以升级脚本本身的改进第一次部署就生效；多次推送会排队，不会并发部署。手动触发时带 `--force`，没有新提交也会重新部署一遍

前提是服务器上的仓库目录是这个仓库的 git clone（不是 `deploy.sh` 那种文件同步），默认路径 `/root/tg-aria2-bot`；装在别处（比如 `/opt/tg-aria2-bot`）的话，在 `Settings → Secrets and variables → Actions → Variables` 里加一个仓库变量 `DEPLOY_PATH`。以下三个仓库 Secret 也要配置（`Settings → Secrets and variables → Actions → Secrets`）：

| Secret | 说明 |
|---|---|
| `DEPLOY_SSH_KEY` | 部署专用私钥（不要用你日常登录用的私钥） |
| `DEPLOY_HOST` | 服务器地址 |
| `DEPLOY_USER` | SSH 用户名 |

部署失败时在 Actions 日志里看「SSH deploy」这一步：`ssh: unable to authenticate` 说明 GitHub 用的密钥被服务器拒绝了——检查 `DEPLOY_SSH_KEY` 是完整的私钥（含 `-----BEGIN` / `-----END` 两行、不带密码短语），对应的公钥在服务器上 `DEPLOY_USER` 的 `~/.ssh/authorized_keys` 里，`DEPLOY_HOST` 指向的是这台服务器；服务器上 `journalctl -u ssh -n 20`（或 `tail /var/log/auth.log`）能看到拒绝的具体原因。

`--reset` 会让服务器上 git 跟踪的文件和 `origin/master` 完全一致，但 `.env`、`data/`、`.venv/`、`downloads/`、`backups/` 这些运行时文件都在 `.gitignore` 里，不受影响；`aria2-config/` 里的本地配置由升级流程备份后原样恢复。想临时关掉自动部署，去 Actions 页面禁用这个 workflow。

### 测试

单元测试覆盖卡片/键盘渲染、aria2 RPC 客户端、任务状态机（TaskManager）、数据库仓储、多节点、Web 鉴权、配置读写等，全部不依赖真实网络和 aria2：

```bash
pip install -r requirements.txt ruff
export BOT_TOKEN=test API_ID=1 API_HASH=test ARIA2_SECRET=test   # 没有 .env 时给配置项塞假值
python -m unittest discover tests -v
ruff check .
```

### 数据库

SQLite（默认 `data/tasks.db`，`aiosqlite` 异步访问）。schema 在 [`bot/db/models.py`](bot/db/models.py)，新增列走 `MIGRATIONS` 列表（幂等的 `ALTER TABLE`，启动时自动执行，重复跑会跳过）。

## 目录结构

```
tg-aria2-bot/
├── tg-aria2.sh                 # 一体化脚本（快捷命令 tg-aria2）：安装 / 升级 / 管理菜单 / 备份恢复 / 回退
├── install.sh, update.sh       # 兼容入口，转发给 tg-aria2.sh install / update
├── deploy.sh                   # 开发调试用：把本地工作区直接同步到服务器
├── systemd/                    # bare 模式用的 unit 模板（含 tg-aria2-web、tg-ariang）
├── docker-compose.yml
├── Dockerfile
├── Dockerfile.ariang            # nginx + 官方 AriaNg 静态构建产物（docker 模式）
├── requirements.txt
├── .env.example
├── tests/                      # 单元测试（无需网络/aria2）
├── aria2-config/                # 预置的 P3TERX/aria2.conf 文件，路径已适配本项目（docker 模式用）
│   ├── aria2.conf                # dir=/downloads, rpc-secret 安装时自动写入（升级时本地改动会保留）
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
    │   ├── callbacks.py            # 按钮回调：导航/列表/任务操作/待确认任务
    │   ├── settings_menu.py        # 设置页（限速/并发/目录/清理/通知等，仅管理员）
    │   ├── admin.py                # 设置页管理功能（白名单/GoFile/rclone/重启/节点）
    │   ├── links.py                # URL / 磁力 / .torrent 消息
    │   └── media.py                # 转发的媒体文件
    ├── core/
    │   ├── aria2_rpc.py            # aria2 JSON-RPC + WebSocket 事件订阅（纯 aiohttp）
    │   ├── aria2_client.py         # 在 RPC 之上封装 Download/File/Stats 对象
    │   ├── node_pool.py            # 多节点（多个 aria2 实例）管理
    │   ├── task_manager.py         # WS 事件 + 3 秒轮询兜底、进度刷新（看的时候快/不看慢）、GoFile 管线、告警
    │   ├── cards.py                # 所有卡片文案渲染
    │   ├── keyboards.py            # 所有 InlineKeyboard 构建
    │   ├── list_view.py            # tab 式任务列表渲染
    │   ├── pending_tasks.py        # 待确认任务（存 SQLite，TTL 30 分钟，重启不丢）
    │   ├── gofile.py               # gofile.io API（含游客 token 自动创建）
    │   ├── compress.py             # zip 压缩/删除
    │   ├── storage.py              # 目录归类、磁盘检查、URL 哈希
    │   ├── telegram_files.py       # 本地 bot-api 绝对路径 → file:// URI 适配
    │   ├── sysinfo.py / stats_view.py  # 服务器状态、下载统计
    │   └── conf_editor.py          # .env / aria2.conf / script.conf 读写
    ├── db/                       # SQLite：schema + 迁移 + 仓储
    ├── middlewares/auth.py       # 白名单校验（env 种子 + DB 动态名单）
    └── web/                      # 自建管理后台：FastAPI + 纯静态 HTML/JS 前端，无构建步骤
        ├── app.py
        ├── auth.py                 # 单密码 + HMAC 签名 cookie，不依赖数据库存 session
        └── static/                 # index.html / app.js / style.css
```

`aria2-config/` 里的文件取自 https://github.com/P3TERX/aria2.conf (MIT License)，调整了路径（`/root/Download` → `/downloads`，`/root/.aria2` → `/config`）以适配本项目的 docker 部署，容器首次启动直接使用这份配置，不需要联网去 GitHub 拉取。
`vendor/` 里的文件是**未经任何修改**的原始副本（路径仍是上游默认的 `/root/...`），存在这里只是为了离线安装（bare 安装会优先用 `vendor/aria2.sh/aria2.sh`）和审计对照，不会被本项目直接引用运行。

**重要**：`aria2-config/` 里不再放 `core`/`clean.sh`/`delete.sh`/`tracker.sh`——实测 `p3terx/aria2-pro` 镜像首次启动会用它自己的这几个文件覆盖到容器内的 `/config/script/`（它的 `core` 把 `ARIA2_CONF_DIR` 写死为 `/config`，不依赖脚本物理路径，比我们原来 vendor 的 `$(dirname $0)` 写法更健壮，所以直接用镜像自带的更省心），放在仓库顶层也会被起容器时清掉，纯属误导。`upload.sh` 是镜像不自带的额外功能，必须放在 `aria2-config/script/upload.sh`（对应容器内 `/config/script/upload.sh`）才能和镜像自己的 `core` 配套工作；`aria2.conf` 里的 `on-download-complete`/`on-download-stop` 也相应指向 `/config/script/*.sh`，不能是 `/config/` 顶层。

## 致谢

- [P3TERX/aria2.conf](https://github.com/P3TERX/aria2.conf) / [P3TERX/aria2.sh](https://github.com/P3TERX/aria2.sh) — aria2 完美配置与安装脚本（MIT）
- [aiogram](https://github.com/aiogram/aiogram) · [AriaNg](https://github.com/mayswind/AriaNg) · [aria2p](https://github.com/pawamoy/aria2p)（早期版本使用，现已换成自带的异步 RPC 客户端）
