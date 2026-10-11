"""没有权限的用户：必须收到提示，提示里要有他自己的 ID 和管理员联系方式。"""
import unittest
from types import SimpleNamespace

from aiogram.types import CallbackQuery, User

from bot.config import settings
from bot.middlewares import auth
from bot.middlewares.auth import AuthMiddleware, denied_text, resolve_admin_contact


class FakeMessage:
    """鸭子类型的 Message：中间件只需要 reply()。注意它没有 .message 属性——
    真正的 aiogram Message 也没有，以前的代码取 event.message 因此永远静默。"""

    def __init__(self):
        self.replies: list[tuple[str, dict]] = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeBot:
    def __init__(self, usernames: dict[int, str | None] | None = None):
        self.usernames = usernames or {}
        self.get_chat_calls = 0

    async def get_chat(self, uid):
        self.get_chat_calls += 1
        if uid not in self.usernames:
            raise RuntimeError("chat not found")
        return SimpleNamespace(username=self.usernames[uid])


class FakeRepo:
    def __init__(self, allowed=()):
        self.allowed = set(allowed)

    async def is_user_allowed(self, uid):
        return uid in self.allowed


class AuthTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = (settings.allowed_user_ids, settings.admin_user_ids, settings.admin_contact)
        settings.allowed_user_ids = "1"
        settings.admin_user_ids = "1"
        settings.admin_contact = ""
        auth._contact_cache = None
        auth._last_denied.clear()
        self.mw = AuthMiddleware()
        self.reached = []

    def tearDown(self):
        settings.allowed_user_ids, settings.admin_user_ids, settings.admin_contact = self._orig
        auth._contact_cache = None
        auth._last_denied.clear()

    async def _handler(self, event, data):
        self.reached.append(event)
        return "handled"

    async def _run(self, event, user_id, *, bot=None, repo=None):
        data = {"event_from_user": SimpleNamespace(id=user_id), "bot": bot, "repo": repo}
        return await self.mw(self._handler, event, data)


class TestDeniedText(unittest.TestCase):
    def test_html_has_copyable_id_and_contact(self):
        text = denied_text(123456789, "@cuiplus", html=True)
        self.assertIn("<code>123456789</code>", text)
        self.assertIn("@cuiplus", text)

    def test_html_without_contact_still_tells_user_what_to_do(self):
        text = denied_text(42, None, html=True)
        self.assertIn("<code>42</code>", text)
        self.assertIn("管理员", text)
        self.assertNotIn("None", text)

    def test_contact_is_html_escaped(self):
        self.assertIn("&lt;b&gt;", denied_text(1, "<b>x</b>", html=True))

    def test_plain_text_for_alert_fits_telegram_limit(self):
        text = denied_text(123456789, "@" + "a" * 300, html=False)
        self.assertLessEqual(len(text), 200)
        self.assertNotIn("<", denied_text(1, "@x", html=False))


class TestDeniedMessage(AuthTestBase):
    async def test_unauthorized_message_gets_id_and_admin_contact(self):
        msg = FakeMessage()
        bot = FakeBot({1: "cuiplus"})
        result = await self._run(msg, 777, bot=bot, repo=FakeRepo())
        self.assertIsNone(result)
        self.assertEqual(self.reached, [])  # 没有放行
        self.assertEqual(len(msg.replies), 1)
        text, kwargs = msg.replies[0]
        self.assertIn("<code>777</code>", text)
        self.assertIn("@cuiplus", text)
        self.assertEqual(kwargs.get("parse_mode"), "HTML")

    async def test_configured_contact_wins_and_skips_api_lookup(self):
        settings.admin_contact = "@my_name"
        bot = FakeBot({1: "someone_else"})
        msg = FakeMessage()
        await self._run(msg, 777, bot=bot, repo=FakeRepo())
        self.assertIn("@my_name", msg.replies[0][0])
        self.assertNotIn("someone_else", msg.replies[0][0])
        self.assertEqual(bot.get_chat_calls, 0)

    async def test_admin_username_lookup_failure_degrades_gracefully(self):
        msg = FakeMessage()
        await self._run(msg, 777, bot=FakeBot({}), repo=FakeRepo())  # get_chat 抛异常
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("<code>777</code>", msg.replies[0][0])

    async def test_repeated_messages_are_rate_limited(self):
        for _ in range(5):
            await self._run(FakeMessage(), 777, bot=FakeBot({1: "a"}), repo=FakeRepo())
        msgs = [FakeMessage() for _ in range(3)]
        for m in msgs:
            await self._run(m, 777, bot=FakeBot({1: "a"}), repo=FakeRepo())
        self.assertEqual(sum(len(m.replies) for m in msgs), 0)  # 一分钟内已回复过

    async def test_different_users_each_get_a_reply(self):
        a, b = FakeMessage(), FakeMessage()
        await self._run(a, 777, bot=FakeBot({1: "x"}), repo=FakeRepo())
        await self._run(b, 888, bot=FakeBot({1: "x"}), repo=FakeRepo())
        self.assertEqual((len(a.replies), len(b.replies)), (1, 1))

    async def test_allowed_user_passes_through(self):
        msg = FakeMessage()
        self.assertEqual(await self._run(msg, 1, bot=FakeBot(), repo=FakeRepo()), "handled")
        self.assertEqual(msg.replies, [])

    async def test_db_whitelisted_user_passes_through(self):
        msg = FakeMessage()
        self.assertEqual(await self._run(msg, 555, bot=FakeBot(), repo=FakeRepo(allowed={555})), "handled")

    async def test_open_bot_when_whitelist_empty(self):
        settings.allowed_user_ids = ""
        self.assertEqual(await self._run(FakeMessage(), 777, bot=FakeBot(), repo=FakeRepo()), "handled")


class TestDeniedCallback(AuthTestBase):
    async def test_button_tap_shows_alert_with_id_and_contact(self):
        answers = []

        class Q(CallbackQuery):
            async def answer(self, text=None, show_alert=None, **kw):
                answers.append((text, show_alert))

        q = Q(id="1", from_user=User(id=777, is_bot=False, first_name="u"), chat_instance="c", data="x")
        await self._run(q, 777, bot=FakeBot({1: "cuiplus"}), repo=FakeRepo())
        text, alert = answers[0]
        self.assertTrue(alert)
        self.assertIn("777", text)
        self.assertIn("@cuiplus", text)
        self.assertNotIn("<", text)


class TestResolveAdminContact(AuthTestBase):
    async def test_multiple_admins_are_listed(self):
        settings.admin_user_ids = "1,2"
        self.assertEqual(await resolve_admin_contact(FakeBot({1: "a", 2: "b"})), "@a 或 @b")

    async def test_admin_without_username_is_skipped(self):
        settings.admin_user_ids = "1,2"
        self.assertEqual(await resolve_admin_contact(FakeBot({1: None, 2: "b"})), "@b")

    async def test_result_is_cached(self):
        bot = FakeBot({1: "a"})
        await resolve_admin_contact(bot)
        await resolve_admin_contact(bot)
        self.assertEqual(bot.get_chat_calls, 1)

    async def test_failure_is_cached_briefly_too(self):
        bot = FakeBot({})
        self.assertIsNone(await resolve_admin_contact(bot))
        await resolve_admin_contact(bot)
        self.assertEqual(bot.get_chat_calls, 1)  # 不会每条陌生消息都去调一次 API

    async def test_falls_back_to_allowed_ids_when_no_admin_ids(self):
        settings.admin_user_ids = ""
        settings.allowed_user_ids = "1"
        self.assertEqual(await resolve_admin_contact(FakeBot({1: "a"})), "@a")


if __name__ == "__main__":
    unittest.main()
