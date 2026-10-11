"""没权限的人「申请使用」→ 管理员一键通过/拒绝。"""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.handlers import access, admin
from bot.handlers.admin import _render_users, decide_access
from bot.middlewares import auth
from bot.middlewares.auth import AdminMiddleware, AuthMiddleware


class FakeBot:
    def __init__(self, undeliverable=()):
        self.sent: list[tuple[int, str, object]] = []
        self.undeliverable = set(undeliverable)

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id in self.undeliverable:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text, reply_markup))


class FakeMessage:
    def __init__(self, html_text="原消息"):
        self.html_text = html_text
        self.edits: list[tuple[str, object]] = []
        self.reply_calls = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))

    async def reply(self, text, **kw):
        self.reply_calls.append((text, kw))


class FakeQuery:
    def __init__(self, data, user_id, *, username=None, full_name="某人", message=None):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, username=username, full_name=full_name)
        self.message = message if message is not None else FakeMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class AccessTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self._orig = (settings.allowed_user_ids, settings.admin_user_ids,
                      settings.access_request_enabled, settings.access_request_cooldown_hours)
        settings.allowed_user_ids = "1"
        settings.admin_user_ids = "1"
        settings.access_request_enabled = True
        settings.access_request_cooldown_hours = 12
        self.bot = FakeBot()

    async def asyncTearDown(self):
        (settings.allowed_user_ids, settings.admin_user_ids,
         settings.access_request_enabled, settings.access_request_cooldown_hours) = self._orig
        await self.repo.close()
        self._dir.cleanup()

    async def _apply(self, user_id=777, **kw):
        q = FakeQuery("access:request", user_id, username=kw.pop("username", "zhangsan"),
                      full_name=kw.pop("full_name", "张三"))
        await access.request_access(q, self.repo, self.bot)
        return q


class TestRepoRequests(AccessTestBase):
    async def test_upsert_and_get(self):
        await self.repo.upsert_access_request(5, "u", "名字", now=100.0)
        row = await self.repo.get_access_request(5)
        self.assertEqual((row["status"], row["username"], row["full_name"]), ("pending", "u", "名字"))

    async def test_only_the_first_decision_wins(self):
        await self.repo.upsert_access_request(5, "u", "n", now=1.0)
        self.assertTrue(await self.repo.decide_access_request(5, "approved", 1, now=2.0))
        self.assertFalse(await self.repo.decide_access_request(5, "rejected", 9, now=3.0))
        row = await self.repo.get_access_request(5)
        self.assertEqual((row["status"], row["decided_by"]), ("approved", 1))

    async def test_list_pending_excludes_decided(self):
        await self.repo.upsert_access_request(5, None, None, now=1.0)
        await self.repo.upsert_access_request(6, None, None, now=2.0)
        await self.repo.decide_access_request(5, "rejected", 1)
        self.assertEqual([r["user_id"] for r in await self.repo.list_pending_requests()], [6])

    async def test_reapplying_resets_to_pending(self):
        await self.repo.upsert_access_request(5, "u", "n", now=1.0)
        await self.repo.decide_access_request(5, "rejected", 1, now=2.0)
        await self.repo.upsert_access_request(5, "u", "n", now=3.0)
        row = await self.repo.get_access_request(5)
        self.assertEqual((row["status"], row["decided_at"], row["notified_at"]), ("pending", None, None))

    async def test_add_allowed_user_keeps_existing_fields_when_readded(self):
        await self.repo.add_allowed_user(5, "备注", username="u", full_name="名字")
        await self.repo.add_allowed_user(5)                      # 重复添加不带任何信息
        row = (await self.repo.list_allowed_users())[0]
        self.assertEqual((row["note"], row["username"], row["full_name"]), ("备注", "u", "名字"))


class TestApplicantSide(AccessTestBase):
    async def test_application_notifies_admins_with_name_id_and_buttons(self):
        q = await self._apply(777)
        self.assertEqual(len(self.bot.sent), 1)
        chat_id, text, markup = self.bot.sent[0]
        self.assertEqual(chat_id, 1)
        self.assertIn("张三", text)
        self.assertIn("@zhangsan", text)
        self.assertIn("<code>777</code>", text)
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(cbs, ["access:ok:777", "access:no:777"])
        # 申请人看到的"没有权限"消息被改成"已提交"，按钮消失
        self.assertIn("申请已提交", q.message.edits[0][0])
        self.assertIsNone(q.message.edits[0][1])

    async def test_all_admins_are_notified(self):
        settings.admin_user_ids = "1,2"
        await self._apply()
        self.assertEqual(sorted(c for c, _, _ in self.bot.sent), [1, 2])

    async def test_falls_back_to_whitelist_when_no_admin_ids(self):
        settings.admin_user_ids = ""
        await self._apply()
        self.assertEqual([c for c, _, _ in self.bot.sent], [1])

    async def test_html_in_names_is_escaped(self):
        await self._apply(full_name="<b>坏人</b>", username=None)
        self.assertIn("&lt;b&gt;坏人&lt;/b&gt;", self.bot.sent[0][1])
        self.assertNotIn("<b>坏人</b>", self.bot.sent[0][1])

    async def test_duplicate_application_is_counted_once(self):
        await self._apply()
        q = await self._apply()
        self.assertEqual(len(self.bot.sent), 1)                  # 管理员只被通知一次
        self.assertIn("已经提交", q.message.edits[0][0])

    async def test_unanswered_application_renotifies_after_a_while(self):
        await self._apply()
        await self.repo.mark_request_notified(777, now=time.time() - access.RENOTIFY_AFTER_SECONDS - 5)
        await self._apply()
        self.assertEqual(len(self.bot.sent), 2)

    async def test_rejected_user_must_wait_for_the_cooldown(self):
        await self._apply()
        await self.repo.decide_access_request(777, "rejected", 1)
        q = await self._apply()
        self.assertEqual(len(self.bot.sent), 1)                  # 没有再打扰管理员
        text, alert = q.answers[-1]
        self.assertTrue(alert)
        self.assertIn("再次申请", text)

    async def test_rejected_user_can_reapply_after_the_cooldown(self):
        await self._apply()
        await self.repo.decide_access_request(777, "rejected", 1, now=time.time() - 13 * 3600)
        await self._apply()
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual((await self.repo.get_access_request(777))["status"], "pending")

    async def test_already_whitelisted_user_is_told_so(self):
        await self.repo.add_allowed_user(777)
        q = await self._apply()
        self.assertEqual(self.bot.sent, [])
        self.assertIn("已经有权限", q.message.edits[0][0])

    async def test_disabled_by_config(self):
        settings.access_request_enabled = False
        q = await self._apply()
        self.assertEqual(self.bot.sent, [])
        self.assertTrue(q.answers[-1][1])

    async def test_no_reachable_admin_is_reported_and_application_retried_later(self):
        self.bot = FakeBot(undeliverable={1})
        q = await self._apply()
        self.assertIn("联系不上管理员", q.answers[-1][0])
        self.assertIsNone((await self.repo.get_access_request(777))["notified_at"])
        # 管理员回来之后再点一次，会重新通知（因为上一次根本没送达）
        self.bot = FakeBot()
        await self._apply()
        self.assertEqual(len(self.bot.sent), 1)


class TestAdminDecision(AccessTestBase):
    async def _decide(self, data, admin_id=1, **kw):
        q = FakeQuery(data, admin_id, username="boss", **kw)
        await decide_access(q, self.repo, self.bot)
        return q

    async def _pending(self, uid=777):
        await self.repo.upsert_access_request(uid, "zhangsan", "张三", now=time.time())

    async def test_approve_adds_to_whitelist_with_names_and_tells_the_applicant(self):
        await self._pending()
        q = await self._decide("access:ok:777")
        self.assertTrue(await self.repo.is_user_allowed(777))
        row = (await self.repo.list_allowed_users())[0]
        self.assertEqual((row["username"], row["full_name"]), ("zhangsan", "张三"))
        self.assertEqual((await self.repo.get_access_request(777))["status"], "approved")
        chat_id, text, _ = self.bot.sent[-1]
        self.assertEqual(chat_id, 777)
        self.assertIn("已通过", text)
        # 管理员那条通知被改成最终结果，按钮去掉
        self.assertIn("已通过", q.message.edits[-1][0])
        self.assertIsNone(q.message.edits[-1][1])

    async def test_reject_does_not_whitelist_and_mentions_cooldown(self):
        await self._pending()
        await self._decide("access:no:777")
        self.assertFalse(await self.repo.is_user_allowed(777))
        self.assertEqual((await self.repo.get_access_request(777))["status"], "rejected")
        self.assertIn("12 小时", self.bot.sent[-1][1])

    async def test_second_click_is_ignored(self):
        await self._pending()
        await self._decide("access:ok:777")
        sent_before = len(self.bot.sent)
        q = await self._decide("access:no:777", admin_id=2)      # 另一个管理员晚了一步
        self.assertTrue(await self.repo.is_user_allowed(777))    # 没被改成拒绝
        self.assertEqual(len(self.bot.sent), sent_before)        # 申请人没有被再通知一次
        self.assertIn("已经处理过", q.answers[-1][0])

    async def test_unknown_request(self):
        q = await self._decide("access:ok:123456")
        self.assertIn("不存在", q.answers[-1][0])
        self.assertFalse(await self.repo.is_user_allowed(123456))

    async def test_blocked_applicant_does_not_break_the_decision(self):
        self.bot = FakeBot(undeliverable={777})
        await self._pending()
        await self._decide("access:ok:777")
        self.assertTrue(await self.repo.is_user_allowed(777))

    async def test_page_variant_refreshes_the_whitelist_page(self):
        await self._pending()
        q = await self._decide("access:pok:777")
        text, _ = q.message.edits[-1]
        self.assertIn("白名单", text)
        self.assertIn("张三", text)                                # 通过后在列表里能看到名字
        self.assertNotIn("待处理的申请", text)

    async def test_decision_buttons_are_registered_on_the_admin_only_router(self):
        self.assertTrue(any(isinstance(m, AdminMiddleware) for m in admin.router.callback_query.middleware))
        for data in ("access:ok:1", "access:no:1", "access:pok:1", "access:pno:1"):
            self.assertTrue(await self._matches(admin.router, data), data)
            self.assertFalse(await self._matches(access.router, data), data)
        self.assertTrue(await self._matches(access.router, "access:request"))
        self.assertFalse(await self._matches(admin.router, "access:request"))

    async def _matches(self, router, data) -> bool:
        from aiogram.types import CallbackQuery, User
        q = CallbackQuery(id="1", from_user=User(id=2, is_bot=False, first_name="u"), chat_instance="c", data=data)
        for handler in router.callback_query.handlers:
            ok, _ = await handler.check(q)
            if ok:
                return True
        return False


class TestWhitelistPage(AccessTestBase):
    async def test_pending_requests_are_listed_with_buttons(self):
        await self.repo.upsert_access_request(777, "zhangsan", "张三", now=time.time())
        text, markup = await _render_users(self.repo)
        self.assertIn("待处理的申请（1）", text)
        self.assertIn("张三", text)
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("access:pok:777", cbs)
        self.assertIn("access:pno:777", cbs)

    async def test_db_users_show_their_names(self):
        await self.repo.add_allowed_user(5, "朋友", username="friend", full_name="小王")
        text, _ = await _render_users(self.repo)
        self.assertIn("小王", text)
        self.assertIn("@friend", text)
        self.assertIn("朋友", text)

    async def test_users_without_names_still_render(self):
        await self.repo.add_allowed_user(5)
        text, _ = await _render_users(self.repo)
        self.assertIn("<code>5</code>", text)

    async def test_adduser_by_hand_resolves_a_pending_request(self):
        await self.repo.upsert_access_request(777, "z", "张", now=time.time())
        msg = FakeMessage()
        cmd = SimpleNamespace(args="777 手动加的")
        msg.from_user = SimpleNamespace(id=1)
        await admin.cmd_adduser(msg, cmd, self.repo)
        self.assertEqual((await self.repo.get_access_request(777))["status"], "approved")
        self.assertEqual(await self.repo.list_pending_requests(), [])


class TestMiddlewareIntegration(AccessTestBase):
    def setUp(self):
        auth._contact_cache = None
        auth._last_denied.clear()

    async def _run(self, event, user_id):
        reached = []

        async def handler(e, d):
            reached.append(e)
            return "handled"

        data = {"event_from_user": SimpleNamespace(id=user_id), "bot": None, "repo": self.repo}
        return await AuthMiddleware()(handler, event, data), reached

    async def test_apply_button_is_let_through_for_strangers(self):
        from aiogram.types import CallbackQuery, User
        q = CallbackQuery(id="1", from_user=User(id=777, is_bot=False, first_name="u"), chat_instance="c",
                          data="access:request")
        result, reached = await self._run(q, 777)
        self.assertEqual(result, "handled")
        self.assertEqual(len(reached), 1)

    async def test_other_buttons_are_still_denied_for_strangers(self):
        from aiogram.types import CallbackQuery, User
        answers = []

        class Q(CallbackQuery):
            async def answer(self, text=None, show_alert=None, **kw):
                answers.append(text)

        q = Q(id="1", from_user=User(id=777, is_bot=False, first_name="u"), chat_instance="c", data="task:pause:g1")
        result, reached = await self._run(q, 777)
        self.assertIsNone(result)
        self.assertEqual(reached, [])
        self.assertIn("没有权限", answers[0])

    async def test_admin_not_in_whitelist_is_still_allowed(self):
        settings.allowed_user_ids = "2"
        settings.admin_user_ids = "9"
        result, _ = await self._run(FakeMessage(), 9)
        self.assertEqual(result, "handled")

    async def test_stranger_message_gets_the_apply_button(self):
        class Msg(FakeMessage):
            async def reply(self, text, **kw):
                self.reply_calls.append((text, kw))

        msg = Msg()
        await self._run(msg, 777)
        text, kw = msg.reply_calls[0]
        self.assertIn("申请使用", text)
        cbs = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(cbs, ["access:request"])

    async def test_no_apply_button_when_disabled(self):
        settings.access_request_enabled = False
        msg = FakeMessage()
        await self._run(msg, 777)
        text, kw = msg.reply_calls[0]
        self.assertNotIn("申请使用", text)
        self.assertIsNone(kw["reply_markup"])


if __name__ == "__main__":
    unittest.main()
