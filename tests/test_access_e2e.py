"""集成测试：真的 aiogram Dispatcher + 主程序同样的路由/中间件装配，只把 Telegram
网络层换成录音机。单测里的替身证明不了"申请使用"按钮真的能穿过权限中间件、
伪造的"通过"真的被拦住——这些取决于 aiogram 的中间件传播规则，只能用真的跑。"""
import datetime
import os
import tempfile
import unittest

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetChat, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.handlers import access, admin, callbacks, commands, links, media, settings_menu
from bot.middlewares.auth import AuthMiddleware
from tests.fakes import FakeNodePool


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=900 + len(self.calls), date=datetime.datetime.now(),
                chat=Chat(id=method.chat_id, type="private"), text=method.text,
            )
        if isinstance(method, GetChat):
            return Chat(id=method.chat_id, type="private", username="boss")
        return True

    async def stream_content(self, *a, **kw):
        yield b""

    async def close(self):
        pass


ROUTERS = (commands.router, access.router, admin.router, settings_menu.router,
           callbacks.router, links.router, media.router)


def _detach_routers():
    """路由是模块级单例，只能挂到一个 Dispatcher 上；每个用例新建 Dispatcher 前后
    都要摘下来，否则第二个用例 include_router 会抛"已经挂载"。"""
    for router in ROUTERS:
        router._parent_router = None


class AccessFlowE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _detach_routers()
        self.addCleanup(_detach_routers)
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        # 注册在 setUp 早期：即使后面的装配抛异常也一定会关库，否则 aiosqlite 的
        # 后台线程不退出，整个测试进程会挂住
        self.addAsyncCleanup(self.repo.close)
        self._orig = (settings.allowed_user_ids, settings.admin_user_ids, settings.access_request_enabled)
        settings.allowed_user_ids = "1"
        settings.admin_user_ids = "1"
        settings.access_request_enabled = True

        from bot.middlewares import auth
        auth._contact_cache = None
        auth._last_denied.clear()

        self.session = RecordingSession()
        self.bot = Bot("123456:ABCDEF", session=self.session)
        self.dp = Dispatcher()
        self.dp.message.middleware(AuthMiddleware())
        self.dp.callback_query.middleware(AuthMiddleware())
        for router in ROUTERS:
            self.dp.include_router(router)
        self.dp["repo"] = self.repo
        self.dp["nodes"] = FakeNodePool()
        self.dp["aria2"] = None
        self.dp["task_manager"] = None

        self.now = datetime.datetime.now()
        self._update_id = 0
        self.stranger = User(id=777, is_bot=False, first_name="张三", username="zhangsan")
        self.admin = User(id=1, is_bot=False, first_name="老板", username="boss")
        self.bot_user = User(id=999, is_bot=True, first_name="bot")

    async def asyncTearDown(self):
        (settings.allowed_user_ids, settings.admin_user_ids, settings.access_request_enabled) = self._orig

    def _update(self, **kw) -> Update:
        self._update_id += 1
        return Update(update_id=self._update_id, **kw)

    async def _say(self, user, text):
        msg = Message(message_id=1, date=self.now, chat=Chat(id=user.id, type="private"), from_user=user, text=text)
        await self.dp.feed_update(self.bot, self._update(message=msg))

    async def _tap(self, user, data):
        msg = Message(message_id=2, date=self.now, chat=Chat(id=user.id, type="private"),
                      from_user=self.bot_user, text="…")
        query = CallbackQuery(id=f"q{self._update_id}", from_user=user, chat_instance="ci", data=data, message=msg)
        await self.dp.feed_update(self.bot, self._update(callback_query=query))

    def _sent(self, chat_id=None):
        return [c for c in self.session.calls
                if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id)]

    def _alerts(self):
        return [c for c in self.session.calls if isinstance(c, AnswerCallbackQuery)]

    async def test_stranger_gets_a_prompt_with_the_apply_button(self):
        await self._say(self.stranger, "/start")
        reply = self._sent(777)[-1]
        self.assertIn("没有权限", reply.text)
        self.assertIn("<code>777</code>", reply.text)
        buttons = [b.callback_data for row in reply.reply_markup.inline_keyboard for b in row]
        self.assertEqual(buttons, ["access:request"])

    async def test_applying_notifies_the_admin_with_name_and_id(self):
        await self._tap(self.stranger, "access:request")
        to_admin = self._sent(1)
        self.assertEqual(len(to_admin), 1)
        self.assertIn("张三", to_admin[0].text)
        self.assertIn("777", to_admin[0].text)
        self.assertEqual((await self.repo.get_access_request(777))["status"], "pending")

    async def test_stranger_cannot_use_any_other_button(self):
        await self._tap(self.stranger, "nav:start")
        alert = self._alerts()[-1]
        self.assertIn("没有权限", alert.text)
        self.assertTrue(alert.show_alert)

    async def test_stranger_cannot_forge_an_approval(self):
        await self._tap(self.stranger, "access:request")
        await self._tap(self.stranger, "access:ok:777")      # 自己给自己点"通过"
        self.assertFalse(await self.repo.is_user_allowed(777))
        self.assertEqual((await self.repo.get_access_request(777))["status"], "pending")

    async def test_admin_approval_whitelists_and_tells_the_applicant(self):
        await self._tap(self.stranger, "access:request")
        await self._tap(self.admin, "access:ok:777")
        self.assertTrue(await self.repo.is_user_allowed(777))
        self.assertIn("已通过", self._sent(777)[-1].text)

    async def test_admin_rejection_does_not_whitelist(self):
        await self._tap(self.stranger, "access:request")
        await self._tap(self.admin, "access:no:777")
        self.assertFalse(await self.repo.is_user_allowed(777))
        self.assertIn("没有通过", self._sent(777)[-1].text)

    async def test_approved_user_can_use_the_bot(self):
        await self._tap(self.stranger, "access:request")
        await self._tap(self.admin, "access:ok:777")
        before = len(self._sent(777))
        await self._say(self.stranger, "/quota")
        self.assertIn("额度", self._sent(777)[before:][-1].text)

    async def test_admin_who_is_not_in_the_whitelist_can_still_use_the_bot(self):
        settings.allowed_user_ids = "2"
        settings.admin_user_ids = "1"
        await self._say(self.admin, "/quota")
        self.assertIn("额度", self._sent(1)[-1].text)


if __name__ == "__main__":
    unittest.main()
