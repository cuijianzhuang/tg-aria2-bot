import unittest

import bot.core.gofile as gofile


class TestSessionLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await gofile.close_session()

    async def test_get_session_reuses_same_instance(self):
        s1 = gofile._get_session()
        s2 = gofile._get_session()
        self.assertIs(s1, s2)

    async def test_close_session_is_safe_when_never_opened(self):
        gofile._session = None
        await gofile.close_session()  # 不应该抛异常

    async def test_get_session_recreates_after_close(self):
        s1 = gofile._get_session()
        await gofile.close_session()
        s2 = gofile._get_session()
        self.assertIsNot(s1, s2)
        self.assertFalse(s2.closed)

    async def test_close_session_is_idempotent(self):
        gofile._get_session()
        await gofile.close_session()
        await gofile.close_session()  # 第二次调用不应该抛异常


if __name__ == "__main__":
    unittest.main()
