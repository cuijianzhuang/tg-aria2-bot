"""RSS 订阅自动下载：每个订阅 = 一个 RSS/Atom 地址 + 关键词过滤，有新条目就自动加任务。

要点
- 新订阅第一次轮询只"记账"（把现有条目标记为见过），不下载——否则订阅一个有几百条
  历史的源会一下子灌几百个任务。之后出现的新条目才下载。
- 条目是否下载只看标题：keyword 里的每个词都要出现，exclude 里任一词出现就跳过
  （不区分大小写）。
- 下载源优先级：enclosure → link → magnetURI 之类的扩展标签；只认 magnet: 和
  http(s)。额度、磁盘空间的检查跟手动添加一样；被额度/空间拒绝的条目不记账，下一轮
  再试。
- XML 来自外部，拒绝带 DOCTYPE/ENTITY 的内容（防实体膨胀），响应体限 2MB。
"""
import asyncio
import ipaddress
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import escape
from urllib.parse import urlparse

import aiohttp

from bot.config import settings
from bot.core import quota, storage

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20
MAX_NEW_PER_POLL = 20     # 一个源一轮最多新增这么多任务，其余留到下一轮
MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:\S+$", re.IGNORECASE)
HTTP_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


class FeedError(Exception):
    """能直接给用户看的错误。"""


@dataclass
class Item:
    guid: str
    title: str
    source: str | None      # 可下载的 magnet:/http(s) 链接，找不到为 None


def validate_url(url: str) -> str:
    """订阅地址必须是 http(s)，且不能直接指向本机/内网 IP 或 localhost。
    （主机名解析到内网的情况这里不拦；要拦需要在连接时再检查。）"""
    url = url.strip()
    if not HTTP_RE.match(url) or len(url) > 1000:
        raise FeedError("订阅地址必须是 http(s):// 开头的链接。")
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise FeedError("不能订阅本机或内网地址。")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
        raise FeedError("不能订阅本机或内网地址。")
    return url


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child_text(el, name: str) -> str:
    for c in el:
        if _local(c.tag) == name and c.text and c.text.strip():
            return c.text.strip()
    return ""


def _usable(url: str | None) -> str | None:
    url = (url or "").strip()
    return url if url and (MAGNET_RE.match(url) or HTTP_RE.match(url)) else None


def _pick_source(entry) -> str | None:
    # 1) enclosure（RSS）/ link rel=enclosure（Atom）
    for c in entry:
        name = _local(c.tag)
        if name == "enclosure" or (name == "link" and c.get("rel") == "enclosure"):
            src = _usable(c.get("url") or c.get("href"))
            if src:
                return src
    # 2) 普通 link（RSS 是文本，Atom 是 href 属性；Atom 的 alternate 链接多半是网页不是文件，
    #    但种子站的 RSS 里它常常就是 .torrent 地址，所以接受）
    for c in entry:
        if _local(c.tag) == "link":
            src = _usable(c.text or c.get("href"))
            if src:
                return src
    # 3) magnetURI / 其它扩展标签里直接放 magnet 的
    for c in entry:
        if _local(c.tag) in ("magneturi", "magnet"):
            src = _usable(c.text)
            if src:
                return src
    return _usable(_child_text(entry, "guid") or _child_text(entry, "id"))


def parse_feed(data: bytes) -> tuple[str, list[Item]]:
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data[:65536].lower():
        raise FeedError("订阅源内容不安全（含 DOCTYPE/ENTITY 声明），已拒绝。")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise FeedError("不是有效的 RSS/Atom 内容。") from None
    channel = next((c for c in root if _local(c.tag) == "channel"), root)
    title = _child_text(channel, "title")
    items = []
    for el in channel.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        item_title = _child_text(el, "title")
        src = _pick_source(el)
        guid = _child_text(el, "guid") or _child_text(el, "id") or src or item_title
        if guid and item_title:
            items.append(Item(guid=guid[:500], title=item_title[:300], source=src))
    return title, items


def _words(text: str) -> list[str]:
    return [w.lower() for w in re.split(r"[\s,，]+", text or "") if w]


def matches(title: str, keyword: str, exclude: str) -> bool:
    low = title.lower()
    return all(w in low for w in _words(keyword)) and not any(w in low for w in _words(exclude))


async def fetch(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, \
                session.get(url, proxy=settings.proxy_url or None, headers={"User-Agent": "tg-aria2-bot"}) as resp:
            if resp.status != 200:
                raise FeedError(f"订阅源返回 HTTP {resp.status}")
            body = await resp.content.read(MAX_BODY_BYTES + 1)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise FeedError(f"访问订阅源失败：{type(e).__name__}") from None
    if len(body) > MAX_BODY_BYTES:
        raise FeedError("订阅源内容太大（超过 2MB）。")
    return body


def _has_space(path: str) -> bool:
    try:
        return storage.has_enough_space(path, 0)
    except OSError:
        return True      # 下载目录还不存在：交给 aria2 去建，别因为这个卡住订阅


async def poll_feed(feed, *, bot, repo, nodes, task_manager=None) -> int:
    """轮询一个订阅，返回新增任务数。出错只记到订阅上（last_error），不抛。"""
    from bot.core.cards import render_task_card
    from bot.core.keyboards import task_keyboard
    from bot.handlers.callbacks import _add_source

    try:
        title, items = parse_feed(await fetch(feed["url"]))
    except FeedError as e:
        await repo.update_feed_state(feed["id"], error=str(e))
        return 0

    seen = await repo.seen_guids(feed["id"])
    baseline = feed["last_checked"] is None
    fresh = [it for it in items if it.guid not in seen]
    if baseline:
        await repo.mark_seen(feed["id"], [it.guid for it in fresh])
        await repo.update_feed_state(feed["id"], title=title or None)
        return 0

    added = 0
    done: list[str] = []
    node = nodes.get_node(feed["node"])
    if node is None or not node.enabled:
        node = nodes.resolve("default")
    # 从旧到新处理：RSS 一般新条目在前
    for it in reversed(fresh):
        if added >= MAX_NEW_PER_POLL:
            break
        if not matches(it.title, feed["keyword"], feed["exclude"]) or not it.source:
            done.append(it.guid)
            continue
        kind = "magnet" if MAGNET_RE.match(it.source) else "url"
        ref = storage.url_hash(it.source)
        if await repo.get_completed_by_source(kind, ref):
            done.append(it.guid)
            continue
        refusal = await quota.admission_error(
            repo, feed["user_id"], None,
            pending_usage=task_manager.pending_usage(feed["user_id"]) if task_manager else 0,
        )
        if refusal or (node.is_local and not _has_space(node.download_dir)):
            break        # 额度/空间不够：这一条和后面的都留到下一轮
        try:
            gid = await _add_source(nodes, node.name, kind, it.source, None if kind == "magnet" else it.title)
        except Exception:
            log.warning("rss: could not add %r from feed %s", it.title, feed["id"], exc_info=True)
            continue     # 添加失败不记账，下一轮重试
        task_id = await repo.create_task(
            gid=gid, user_id=feed["user_id"], chat_id=feed["chat_id"], reply_message_id=None,
            source_type=kind, source_ref=ref, file_name=it.title if kind == "magnet" else None,
            file_size=None, payload=it.source, node=node.name,
        )
        done.append(it.guid)
        added += 1
        row = await repo.get_by_id(task_id)
        try:
            msg = await bot.send_message(
                chat_id=feed["chat_id"],
                text=f"📡 <b>RSS 新条目</b>：{escape(it.title, quote=False)}\n\n"
                     + render_task_card(row, status="PENDING", node_label=nodes.label(node.name)),
                parse_mode="HTML", reply_markup=task_keyboard(gid, "PENDING", local=node.is_local),
            )
            await repo.set_reply_message(task_id, msg.message_id)
            if task_manager:
                task_manager.watch(gid)
        except Exception:
            log.info("rss: could not announce task %s", task_id)
    await repo.mark_seen(feed["id"], done)
    await repo.update_feed_state(feed["id"], title=title or None)
    return added


async def rss_loop(bot, repo, nodes, task_manager=None, *, first_delay: float = 60.0):
    await asyncio.sleep(first_delay)
    while True:
        try:
            for feed in await repo.list_feeds(enabled_only=True):
                try:
                    await poll_feed(feed, bot=bot, repo=repo, nodes=nodes, task_manager=task_manager)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("rss poll failed for feed %s", feed["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("rss loop iteration failed")
        await asyncio.sleep(max(1, settings.rss_check_minutes) * 60)


