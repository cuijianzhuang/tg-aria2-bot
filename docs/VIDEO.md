# 视频下载与扫码账号

## 能力和默认行为

视频功能默认关闭。启用后，常见视频站点的页面链接自动进入解析流程；其他站点使用
`/video https://…`。普通下载链接、磁力、种子、Metalink 和 RSS 保持原流程。
视频仅在机器人本机下载，远程 aria2 节点不需要安装视频组件。

解析默认优先 yt-dlp，失败后尝试 you-get；开始下载后不自动换后端。
确认卡可切换后端、画质和账号，默认最佳画质。切换后重新解析，确认时展示实际选择。
yt-dlp 的分离音视频由 FFmpeg 合并为 MKV（不重新编码），you-get 使用其原生合并流程。
仅支持单视频；解析结果标记为播放列表或直播时会拒绝，不提供直播录制能力。不同站点的实际能力取决于解析器版本，
扫码成功不等于当前解析器一定支持该内容，账号也不会改变内容本身的访问权限。

下载阶段默认连续 180 秒没有字节增长会终止并显示原因，暂停和合并不计入此超时。

每个视频拥有独立目录 `DOWNLOAD_DIR/video/video_<id>/`。下载、合并均支持暂停和继续；
暂停通过 Linux 进程组实现，保留执行槽位（默认最多两个），不是释放并发槽位。
取消会终止整个进程组；取消并删文件需二次确认。重启后未完成任务转为暂停，继续时
重新解析并尝试利用分片，部分站点或格式可能需要重新下载。合并结果通过 FFprobe 检查。
每日额度使用下载阶段的字节增长；you-get 无结构化计数时以工作目录增长估算，
不是操作系统网络计量，重传和站点额外请求不保证精确统计；合并副本不重复计费。

## Docker

普通 bot 镜像内置固定摘要的 FFmpeg/FFprobe、Deno，以及锁定版本的两个解析器。
扫码 Chromium 独立放在 login 镜像中，没有浏览器调试或登录服务公网端口。

```dotenv
VIDEO_ENABLED=true
VIDEO_LOGIN_ENABLED=true
VIDEO_CONCURRENCY=2
VIDEO_PROBE_CONCURRENCY=2
VIDEO_PROBE_TIMEOUT=45
VIDEO_STALL_TIMEOUT=180
VIDEO_PUBLIC_URL=https://你的后台域名
COMPOSE_PROFILES=web,video-login
```

源码 Docker 模式更新并启动：

```bash
docker compose up -d --build
```

固定镜像模式：先发布工作流成功生成 bot/login/ariang 同版本镜像，再运行
`bash scripts/deploy-images.sh v版本号`。只有启用 video-login profile 时才拉取并锁定
扫码镜像摘要。现有源码安装不会被切换到镜像模式。

## 裸机源码部署

```bash
# 下载能力：自动安装 FFmpeg、Python 视频依赖、校验后的 Deno（装到项目 venv/bin）。
sudo bash tg-aria2.sh install --mode bare --with-video

# 同时安装 Chromium 与独立 systemd 扫码服务。
sudo bash tg-aria2.sh install --mode bare --with-video-login
```

Chromium 的官方依赖安装适用于 Playwright 支持的 Debian/Ubuntu 环境；其他发行版
建议让 bot 保持裸机源码部署，扫码端使用独立容器。共享同一份 `data/` 和 `.env`，
容器中的 `DB_PATH` 固定为 `/app/data/tasks.db`；不要再同时启动另一份 bot。

手动安装使用 `requirements-video.txt` 和 `requirements-login.txt`；
`python -m playwright install --with-deps chromium` 安装浏览器依赖。
源码升级会更新已启用的视频依赖；更改扫码配置后重启 `tg-aria2-video-login` 服务。

## 私聊与 Web 扫码

`/videoaccounts` 或设置里的“视频平台账号”支持 B站、抖音、小红书。
私聊列表展示自己的个人账号、共享账号、最近登录时间与默认身份；可切换默认身份、重新扫码，
移除前需要二次确认。Web 后台展示共享账号详情与共享开关，不列出其他用户的个人凭据。
个人账号绑定 Telegram 用户。个人账号缺失时先展示未登录预览；选择个人账号但失效时
明确报错，不自动切换共享身份。共享账号只有管理员可扫码、移除和开放，重新登录后
需要重新开放。用户在确认卡主动选择共享账号，选择按平台记忆。

管理员也可从 Web 后台的“视频平台账号”进入共享账号管理。
普通用户从私聊获得五分钟一次性 Web 链接，只访问当前扫码会话，不获得后台权限。
链接令牌放在 URL fragment 中，不发往访问日志，兑换后立即从地址移除。
扫码页面的 Cookie 是该页面的短期访问会话，平台原始 Cookie 不返回浏览器。

B站采用平台二维码接口并验证登录后的用户身份；抖音、小红书使用独立 Chromium 上下文，
验证登录后个人入口。页面变动、机房 IP 限制或额外验证码会导致失败，提供刷新、取消和
“需额外验证”提示，不自动破解验证。检测需看到持续存在的可见验证提示，不以验证码组件的类名判断。
平台也可能在生成二维码前就要求验证，手机 App 上的验证不一定能解除服务器浏览器的限制。
失败时可由管理员配置共享 Cookie 文件：

```dotenv
VIDEO_COOKIE_FILE_BILIBILI=/app/data/video-cookies/bilibili.txt
VIDEO_COOKIE_FILE_DOUYIN=/app/data/video-cookies/douyin.txt
VIDEO_COOKIE_FILE_XHS=/app/data/video-cookies/xhs.txt
```

文件为 Netscape 格式，管理员需先开放共享，再由用户显式选择共享模式；关闭共享同样会禁用配置文件凭据。扫码账号存在但未开放时，
不会通过文件绕过共享开关。扫码和 Cookie 都不在 Telegram 群聊传递。

## 数据与维护

- `data/video-private/key`：Fernet 加密密钥，0600，必须与数据库一起备份。
- 数据库视频账号表仅存加密 Cookie、账号标识、归属及共享状态。
- 下载进程使用 0600 临时 Netscape Cookie 文件，结束后删除；启动时清理上次崩溃遗留文件。
- 每个扫码请求隔离浏览器上下文，最多两个并发、五分钟超时，结束后清理二维码。
- “移除本机登录”删除保存的凭据并取消对应未完成扫码；运行中的下载持有独立快照，
  任务结束后清理，不声称注销平台上的所有会话。
- key 丢失时不能解密旧凭据，需要恢复备份或重新扫码，不能生成新密钥后当成旧凭据有效。

`python -m bot.preflight` 在视频启用时检查两个解析器、FFmpeg/FFprobe 与 JS 运行时。
扫码 worker 通过数据库队列接收请求，不开放控制接口；应只运行一个 worker 实例。
查看日志使用 `docker compose logs video-login` 或 `journalctl -u tg-aria2-video-login`。

## 验证边界

自动测试覆盖适配器、任务持久化、进程控制、额度接入、Web授权和扫码状态隔离。
使用本机合成视频验证真实 yt-dlp/you-get 解析和下载；不以此宣称真实视频站点均可用。
实际扫码需要账号本人操作；B站、抖音、小红书的完整扫码成功和目标视频下载需要部署后
逐站验收，尤其需检查当前平台页面与选择器、Cookie有效期及服务器网络可达性。

安装扫码浏览器后，可运行 `RUN_VIDEO_BROWSER_TESTS=1 python -m unittest tests.test_qr_recovery -v`
复现本地 Chromium 回归，覆盖验证码容器误判、短暂/持续验证提示、iframe 和隐藏二维码；不访问真实平台或账号。

### 扫码失败时

机器人和 Web 会区分浏览器未安装、缺少系统依赖、启动失败、平台访问限制、网络超时和二维码未找到。
浏览器缺失或依赖不完整时，systemd 部署可在项目目录重新运行
`sudo bash tg-aria2.sh install --mode bare --with-video-login`；Docker 部署需重新构建/拉取 login 镜像并重启扫码服务。
查看错误类型：`sudo journalctl -u tg-aria2-video-login -n 40 --no-pager`。
日志只记录平台、固定原因代码及异常类型，界面使用固定原因文案，不展示代理凭据、Cookie 或原始浏览器命令。
平台页面受限或要求额外验证时不能靠重复刷新保证成功，需按提示检查服务器网络或使用已有 Cookie 配置。
