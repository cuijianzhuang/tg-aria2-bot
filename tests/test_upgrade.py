"""在机器人里升级：检查更新、起升级任务、重启后汇报结果、界面。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.core import upgrade
from bot.core.upgrade import Outcome, UpdateInfo, UpgradeError, Version
from bot.handlers import upgrade as ui
from bot.middlewares.auth import AdminMiddleware


def cur(short="aaaaaaa", sha=None, branch="master"):
    return Version(sha or short.ljust(40, "0"), short, "当前版本说明", int(time.time()) - 7200, branch)


def info(**kw):
    base = {
        "current": cur(), "remote_sha": "b" * 40, "total": 2,
        "commits": [upgrade.Commit("bbbbbbb", "新功能", int(time.time()) - 60),
                    upgrade.Commit("ccccccc", "修复 <b>", int(time.time()) - 7200)],
    }
    base.update(kw)
    return UpdateInfo(**base)


class UpgradeTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._orig = (settings.db_path, settings.upgrade_enabled, settings.update_check_enabled,
                      settings.admin_user_ids, settings.allowed_user_ids, upgrade.REPO_DIR)
        settings.db_path = os.path.join(self._dir.name, "data", "tasks.db")
        settings.upgrade_enabled = True
        settings.update_check_enabled = True
        settings.admin_user_ids = "1"
        settings.allowed_user_ids = "1"
        upgrade._watching = False

    def tearDown(self):
        (settings.db_path, settings.upgrade_enabled, settings.update_check_enabled,
         settings.admin_user_ids, settings.allowed_user_ids, upgrade.REPO_DIR) = self._orig
        upgrade._watching = False


class FakeBot:
    def __init__(self, edit_fails=False):
        self.edits, self.sent = [], []
        self.edit_fails = edit_fails

    async def edit_message_text(self, **kw):
        if self.edit_fails:
            raise RuntimeError("message to edit not found")
        self.edits.append(kw)

    async def send_message(self, **kw):
        self.sent.append(kw)


# ---------------------------------------------------------------- 能不能升级

class TestSupport(UpgradeTestBase):
    def _repo(self, with_git=True, with_script=True):
        repo = Path(self._dir.name) / "repo"
        repo.mkdir(exist_ok=True)
        if with_git:
            (repo / ".git").mkdir(exist_ok=True)
        if with_script:
            (repo / upgrade.SCRIPT_NAME).write_text("#!/bin/bash\n")
        upgrade.REPO_DIR = repo

    def test_supported_when_everything_is_there(self):
        self._repo()
        with patch("os.geteuid", return_value=0), patch("shutil.which", return_value="/usr/bin/x"):
            self.assertEqual(upgrade.support(), (True, ""))

    def test_disabled_by_config(self):
        self._repo()
        settings.upgrade_enabled = False
        ok, reason = upgrade.support()
        self.assertFalse(ok)
        self.assertIn("UPGRADE_ENABLED", reason)

    def test_docker_image_has_no_git_repo(self):
        self._repo(with_git=False)
        ok, reason = upgrade.support()
        self.assertFalse(ok)
        self.assertIn("docker", reason)
        self.assertIn("sudo tg-aria2 update", reason)     # 告诉用户该怎么办

    def test_old_checkout_without_the_script(self):
        self._repo(with_script=False)
        ok, reason = upgrade.support()
        self.assertFalse(ok)
        self.assertIn("手动升级一次", reason)

    def test_not_root(self):
        self._repo()
        with patch("os.geteuid", return_value=1000):
            self.assertIn("root", upgrade.support()[1])

    def test_missing_systemd_run(self):
        self._repo()
        with patch("os.geteuid", return_value=0), patch("shutil.which", return_value=None):
            self.assertIn("systemd-run", upgrade.support()[1])


# ---------------------------------------------------------------- 检查更新

class FakeGit:
    """按子命令返回预设输出的 git 替身。"""

    def __init__(self, **over):
        self.calls = []
        self.out = {
            "log -1": (0, "a" * 40 + "\taaaaaaa\t1700000000\t当前版本说明"),
            "rev-parse --abbrev-ref": (0, "master"),
            "fetch": (0, ""),
            "rev-parse origin/master": (0, "b" * 40),
            "rev-list --count": (0, "2"),
            "merge-base": (0, ""),
            "log -10": (0, "bbbbbbb\t1700000100\t新功能\nccccccc\t1700000050\t修复"),
            "diff --quiet HEAD origin/master -- requirements.txt": (0, ""),
            "diff --quiet HEAD origin/master -- .env.example": (0, ""),
            "status": (0, ""),
        }
        self.out.update(over)

    async def __call__(self, *args, **kw):
        self.calls.append(args)
        joined = " ".join(args[1:]) if args[0] == "git" else " ".join(args)
        # 最长的前缀优先，避免 "log -1" 抢了 "log -10"
        for key in sorted(self.out, key=len, reverse=True):
            if joined.startswith(key):
                return self.out[key]
        return 0, ""


class TestCheck(UpgradeTestBase):
    def _patch(self, **over):
        git = FakeGit(**over)
        p1 = patch.object(upgrade, "_run", git)
        p2 = patch.object(upgrade, "support", return_value=(True, ""))
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return git

    async def test_new_commits_are_listed(self):
        self._patch()
        i = await upgrade.check()
        self.assertEqual((i.current.short, i.remote_short, i.total), ("aaaaaaa", "bbbbbbb", 2))
        self.assertEqual([c.short for c in i.commits], ["bbbbbbb", "ccccccc"])
        self.assertFalse(i.up_to_date)

    async def test_up_to_date(self):
        self._patch(**{"rev-parse origin/master": (0, "a" * 40), "rev-list --count": (0, "0")})
        self.assertTrue((await upgrade.check()).up_to_date)

    async def test_dependency_and_env_changes_are_detected(self):
        self._patch(**{
            "diff --quiet HEAD origin/master -- requirements.txt": (1, ""),
            "diff --quiet HEAD origin/master -- .env.example": (1, ""),
        })
        i = await upgrade.check()
        self.assertTrue(i.deps_changed)
        self.assertTrue(i.env_changed)

    async def test_local_changes_are_reported(self):
        self._patch(status=(0, " M install.sh\n M bot/main.py"))
        self.assertEqual((await upgrade.check()).dirty, ["install.sh", "bot/main.py"])

    async def test_aria2_config_is_not_a_local_change(self):
        git = self._patch()
        await upgrade.check()
        status_call = next(c for c in git.calls if "status" in c)
        self.assertIn(":!aria2-config", status_call)

    async def test_diverged_branch_is_detected(self):
        self._patch(**{"merge-base": (1, "")})
        self.assertTrue((await upgrade.check()).diverged)

    async def test_fetch_failure_is_explained(self):
        self._patch(fetch=(128, "fatal: unable to access 'https://github.com/x': Could not resolve host"))
        with self.assertRaises(UpgradeError) as cm:
            await upgrade.check()
        self.assertIn("git fetch 失败", str(cm.exception))
        self.assertIn("Could not resolve host", str(cm.exception))

    async def test_detached_head(self):
        self._patch(**{"rev-parse --abbrev-ref": (0, "HEAD")})
        with self.assertRaises(UpgradeError) as cm:
            await upgrade.check()
        self.assertIn("detached HEAD", str(cm.exception))

    async def test_unsupported_environment_raises_the_reason(self):
        with patch.object(upgrade, "support", return_value=(False, "这不是 git 部署")):
            with self.assertRaises(UpgradeError) as cm:
                await upgrade.check()
        self.assertEqual(str(cm.exception), "这不是 git 部署")


# ---------------------------------------------------------------- 起升级任务

class TestStartUpgrade(UpgradeTestBase):
    def _env(self, running=False, run_rc=0):
        calls = []

        async def fake_run(*args, **kw):
            calls.append(args)
            if args[0] == "systemctl":
                return (0, "active" if running else "inactive")
            return run_rc, "" if run_rc == 0 else "Failed to start transient service"

        for p in (patch.object(upgrade, "_run", fake_run), patch.object(upgrade, "support", return_value=(True, ""))):
            p.start()
            self.addCleanup(p.stop)
        upgrade.REPO_DIR = Path("/opt/tg-aria2-bot")
        return calls

    async def _start(self, **kw):
        await upgrade.start_upgrade(chat_id=42, message_id=7, user_id=1, from_sha="a" * 40, target_sha="b" * 40, **kw)

    async def test_runs_in_its_own_systemd_unit_so_the_bot_restart_cannot_kill_it(self):
        calls = self._env()
        await self._start()
        cmd = next(c for c in calls if c[0] == "systemd-run")
        self.assertIn(f"--unit={upgrade.UNIT}", cmd)
        self.assertIn("--collect", cmd)
        self.assertIn("update -y", upgrade.RUNNER)             # 非交互
        self.assertNotIn("--reset", cmd)

    async def test_job_file_records_who_and_what(self):
        self._env()
        await self._start()
        job = upgrade.read_job()
        self.assertEqual((job["chat_id"], job["message_id"], job["from_sha"]), (42, 7, "a" * 40))
        self.assertEqual(job["target_sha"], "b" * 40)

    async def test_reset_flag_is_passed_through(self):
        calls = self._env()
        await self._start(reset=True)
        self.assertEqual(next(c for c in calls if c[0] == "systemd-run")[-1], "--reset")

    async def test_no_user_input_reaches_the_command_line(self):
        calls = self._env()
        await self._start()
        cmd = next(c for c in calls if c[0] == "systemd-run")
        # 命令行只有固定值 + 路径；sha、用户名这些都只进任务文件，不进命令
        self.assertFalse(any("a" * 40 in part or "b" * 40 in part for part in cmd))

    async def test_refuses_a_second_upgrade_while_one_is_running(self):
        self._env(running=True)
        with self.assertRaises(UpgradeError) as cm:
            await self._start()
        self.assertIn("已经有一个升级在进行中", str(cm.exception))
        self.assertIsNone(upgrade.read_job())

    async def test_refuses_when_a_job_is_already_recorded(self):
        self._env()
        await self._start()
        with self.assertRaises(UpgradeError):
            await self._start()

    async def test_launch_failure_cleans_up_and_explains(self):
        self._env(run_rc=1)
        with self.assertRaises(UpgradeError) as cm:
            await self._start()
        self.assertIn("启动升级任务失败", str(cm.exception))
        self.assertIsNone(upgrade.read_job())               # 没有留下假的"升级中"

    async def test_oversized_log_is_rotated(self):
        self._env()
        log = upgrade.log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(b"x" * (upgrade.LOG_MAX_BYTES + 1))
        await self._start()
        self.assertFalse(log.exists())


# ---------------------------------------------------------------- 结果判定与汇报

class TestOutcome(UpgradeTestBase):
    def test_version_changed_is_success(self):
        self.assertEqual(upgrade.classify({"from_sha": "a"}, "b", "").kind, "success")

    def test_already_latest(self):
        self.assertEqual(upgrade.classify({"from_sha": "a"}, "a", "[update] 已经是最新版本（x）").kind, "uptodate")

    def test_unchanged_version_is_failure_including_auto_rollback(self):
        self.assertEqual(upgrade.classify({"from_sha": "a"}, "a", "[warn] 升级失败 —— 自动回滚").kind, "failed")

    def test_success_text_shows_versions_and_subject(self):
        text = upgrade.render_outcome(Outcome("success", "a" * 40, "b" * 40), "修复 <b>x</b>")
        self.assertIn("升级完成", text)
        self.assertIn("aaaaaaa", text)
        self.assertIn("bbbbbbb", text)
        self.assertIn("&lt;b&gt;", text)                    # 提交说明被转义

    def test_failure_text_includes_log_tail_escaped(self):
        text = upgrade.render_outcome(Outcome("failed", "a" * 40, "a" * 40, "ImportError: <module 'x'>"))
        self.assertIn("升级失败", text)
        self.assertIn("&lt;module", text)
        self.assertIn("自动回滚", text)

    def test_log_tail_only_covers_the_latest_run_and_strips_colors(self):
        log = upgrade.log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "=== 2026-10-10 10:00:00 upgrade start ===\n上一次的内容\n"
            "=== 2026-10-11 10:00:00 upgrade start ===\n\x1b[1;32m[update]\x1b[0m 这一次的内容\n",
            encoding="utf-8",
        )
        tail = upgrade.log_tail()
        self.assertIn("这一次的内容", tail)
        self.assertNotIn("上一次", tail)
        self.assertNotIn("\x1b", tail)


class TestFinishPendingUpgrade(UpgradeTestBase):
    def _env(self, *, running_for=0, current_sha="b" * 40):
        """running_for: 前几次轮询时升级任务还在跑。"""
        state = {"polls": 0}

        async def running():
            state["polls"] += 1
            return state["polls"] <= running_for

        async def version():
            return Version(current_sha, current_sha[:7], "新版本说明", 0, "master")

        for p in (patch.object(upgrade, "unit_running", running), patch.object(upgrade, "current_version", version)):
            p.start()
            self.addCleanup(p.stop)
        return state

    def _job(self, **kw):
        job = {"chat_id": 42, "message_id": 7, "user_id": 1, "from_sha": "a" * 40,
               "target_sha": "b" * 40, "started_at": time.time()}
        job.update(kw)
        upgrade.write_job(job)

    async def test_nothing_to_do_without_a_job(self):
        self._env()
        self.assertFalse(await upgrade.finish_pending_upgrade(FakeBot(), poll_seconds=0))

    async def test_success_edits_the_original_message_and_clears_the_job(self):
        self._env()
        self._job()
        bot = FakeBot()
        self.assertTrue(await upgrade.finish_pending_upgrade(bot, poll_seconds=0))
        self.assertEqual((bot.edits[0]["chat_id"], bot.edits[0]["message_id"]), (42, 7))
        self.assertIn("升级完成", bot.edits[0]["text"])
        self.assertIsNone(upgrade.read_job())

    async def test_waits_for_the_upgrade_task_before_reporting(self):
        # 新进程一启动升级任务多半还在做健康检查——这时报成功是错的（健康检查失败会回滚）
        state = self._env(running_for=3)
        self._job()
        bot = FakeBot()
        await upgrade.finish_pending_upgrade(bot, poll_seconds=0)
        self.assertGreater(state["polls"], 3)
        self.assertEqual(len(bot.edits), 1)

    async def test_rollback_is_reported_as_failure(self):
        self._env(current_sha="a" * 40)                     # 版本回到了升级前
        self._job()
        bot = FakeBot()
        await upgrade.finish_pending_upgrade(bot, poll_seconds=0)
        self.assertIn("升级失败", bot.edits[0]["text"])

    async def test_timeout_is_reported(self):
        self._env(running_for=10**9, current_sha="a" * 40)
        self._job()
        bot = FakeBot()
        await upgrade.finish_pending_upgrade(bot, poll_seconds=0, max_wait=-1)
        self.assertIn("升级超时", bot.edits[0]["text"])

    async def test_falls_back_to_a_new_message_when_the_original_is_gone(self):
        self._env()
        self._job()
        bot = FakeBot(edit_fails=True)
        await upgrade.finish_pending_upgrade(bot, poll_seconds=0)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0]["chat_id"], 42)

    async def test_stale_job_is_discarded_silently(self):
        self._env()
        self._job(started_at=time.time() - upgrade.STALE_JOB_SECONDS - 1)
        bot = FakeBot()
        self.assertFalse(await upgrade.finish_pending_upgrade(bot, poll_seconds=0))
        self.assertEqual((bot.edits, bot.sent), ([], []))
        self.assertIsNone(upgrade.read_job())

    async def test_only_one_watcher_reports(self):
        self._env()
        self._job()
        upgrade._watching = True                            # 已经有一个在汇报
        self.assertFalse(await upgrade.finish_pending_upgrade(FakeBot(), poll_seconds=0))
        self.assertIsNotNone(upgrade.read_job())            # 任务文件留给那一个

    async def test_corrupt_job_file_is_ignored(self):
        self._env()
        upgrade.job_path().parent.mkdir(parents=True, exist_ok=True)
        upgrade.job_path().write_text("{not json")
        self.assertFalse(await upgrade.finish_pending_upgrade(FakeBot(), poll_seconds=0))


# ---------------------------------------------------------------- 定期检查

class TestUpdateNotice(UpgradeTestBase):
    def _env(self, update_info, *, busy=False):
        for p in (
            patch.object(upgrade, "support", return_value=(True, "")),
            patch.object(upgrade, "check", AsyncMock(return_value=update_info) if not isinstance(update_info, Exception)
                         else AsyncMock(side_effect=update_info)),
            patch.object(upgrade, "unit_running", AsyncMock(return_value=busy)),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def test_admin_is_told_once_per_new_version(self):
        self._env(info())
        bot = FakeBot()
        self.assertTrue(await upgrade.check_and_notify(bot))
        self.assertEqual(bot.sent[0]["chat_id"], 1)
        self.assertIn("发现新版本", bot.sent[0]["text"])
        self.assertIn("&lt;b&gt;", bot.sent[0]["text"])    # 提交说明被转义
        self.assertFalse(await upgrade.check_and_notify(bot))   # 同一个版本不再提醒
        self.assertEqual(len(bot.sent), 1)

    async def test_a_newer_version_notifies_again(self):
        self._env(info())
        bot = FakeBot()
        await upgrade.check_and_notify(bot)
        self._env(info(remote_sha="c" * 40))
        self.assertTrue(await upgrade.check_and_notify(bot))

    async def test_up_to_date_says_nothing(self):
        self._env(info(total=0, remote_sha=cur().sha))
        self.assertFalse(await upgrade.check_and_notify(FakeBot()))

    async def test_disabled_by_config(self):
        settings.update_check_enabled = False
        self._env(info())
        self.assertFalse(await upgrade.check_and_notify(FakeBot()))

    async def test_silent_while_an_upgrade_is_running(self):
        self._env(info(), busy=True)
        self.assertFalse(await upgrade.check_and_notify(FakeBot()))

    async def test_check_failure_is_not_fatal(self):
        self._env(UpgradeError("git fetch 失败"))
        self.assertFalse(await upgrade.check_and_notify(FakeBot()))

    async def test_not_marked_as_notified_when_nobody_could_be_reached(self):
        self._env(info())

        class Unreachable(FakeBot):
            async def send_message(self, **kw):
                raise RuntimeError("blocked")

        self.assertFalse(await upgrade.check_and_notify(Unreachable()))
        self.assertFalse(upgrade.state_path().exists())     # 下次还会再试

    async def test_all_admins_are_notified(self):
        settings.admin_user_ids = "1,2"
        self._env(info())
        bot = FakeBot()
        await upgrade.check_and_notify(bot)
        self.assertEqual(sorted(m["chat_id"] for m in bot.sent), [1, 2])


# ---------------------------------------------------------------- 界面

def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestRender(unittest.TestCase):
    def test_new_commits_screen(self):
        text, kb = ui.render_check(info(deps_changed=True))
        self.assertIn("发现 2 个新提交", text)
        self.assertIn("依赖有变化", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("upgrade:ask:bbbbbbb", _cbs(kb))

    def test_up_to_date_has_no_upgrade_button(self):
        text, kb = ui.render_check(info(total=0, remote_sha=cur().sha))
        self.assertIn("已经是最新版本", text)
        self.assertFalse(any(c.startswith("upgrade:") for c in _cbs(kb)))

    def test_local_changes_block_the_normal_upgrade_and_offer_a_clearly_labelled_reset(self):
        text, kb = ui.render_check(info(dirty=["install.sh"]))
        self.assertIn("本地改动", text)
        self.assertIn("install.sh", text)
        self.assertEqual([c for c in _cbs(kb) if c.startswith("upgrade:")], ["upgrade:ask:bbbbbbb:reset"])

    def test_diverged_branch_is_flagged(self):
        text, kb = ui.render_check(info(diverged=True))
        self.assertIn("已经分叉", text)
        self.assertIn("upgrade:ask:bbbbbbb:reset", _cbs(kb))

    def test_confirm_screen_explains_the_consequences(self):
        text, kb = ui.render_confirm(info(), reset=False)
        for must in ("备份", "不受影响", "自动回滚"):
            self.assertIn(must, text)
        self.assertIn("upgrade:go:bbbbbbb", _cbs(kb))

    def test_confirm_screen_for_reset_lists_what_will_be_discarded(self):
        text, kb = ui.render_confirm(info(dirty=["install.sh"]), reset=True)
        self.assertIn("永久丢弃", text)
        self.assertIn("install.sh", text)
        self.assertIn("upgrade:go:bbbbbbb:reset", _cbs(kb))

    def test_error_screen(self):
        text, kb = ui.render_error("git fetch 失败 <x>")
        self.assertIn("&lt;x&gt;", text)
        self.assertIn("admin:upgrade", _cbs(kb))

    def test_callback_data_fits_telegram_limit(self):
        _, kb = ui.render_check(info(dirty=["x"]))
        for c in _cbs(kb):
            self.assertLessEqual(len(c.encode()), 64)


class FakeMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=42)
        self.message_id = 7
        self.texts = []

    async def edit_text(self, text, **kw):
        self.texts.append((text, kw.get("reply_markup")))

    async def answer(self, text, **kw):
        self.texts.append((text, kw.get("reply_markup")))

    async def reply(self, text, **kw):
        self.texts.append((text, kw.get("reply_markup")))


class FakeQuery:
    def __init__(self, data, user_id=1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class TestHandlers(UpgradeTestBase):
    def _check(self, result):
        side = result if isinstance(result, Exception) else None
        mock = AsyncMock(return_value=None if side else result, side_effect=side)
        p = patch.object(upgrade, "check", mock)
        p.start()
        self.addCleanup(p.stop)

    def _start(self, side_effect=None):
        mock = AsyncMock(side_effect=side_effect)
        p = patch.object(upgrade, "start_upgrade", mock)
        p.start()
        self.addCleanup(p.stop)
        return mock

    async def test_go_starts_the_upgrade_with_what_was_confirmed(self):
        self._check(info())
        start = self._start()
        q = FakeQuery("upgrade:go:bbbbbbb")
        with patch.object(upgrade, "finish_pending_upgrade", AsyncMock()):
            await ui.upgrade_action(q, bot=FakeBot())
        kw = start.await_args.kwargs
        self.assertEqual((kw["chat_id"], kw["message_id"], kw["user_id"]), (42, 7, 1))
        self.assertEqual((kw["from_sha"], kw["target_sha"], kw["reset"]), (cur().sha, "b" * 40, False))
        self.assertIn("正在升级", q.message.texts[-1][0])
        self.assertIsNone(q.message.texts[-1][1])           # 升级中不留按钮，免得重复点

    async def test_stale_confirmation_asks_again_instead_of_upgrading_to_something_else(self):
        self._check(info(remote_sha="d" * 40))              # 确认之后远端又有了新提交
        start = self._start()
        q = FakeQuery("upgrade:go:bbbbbbb")
        await ui.upgrade_action(q, bot=FakeBot())
        start.assert_not_awaited()
        self.assertIn("又有了新提交", q.message.texts[-1][0])

    async def test_ask_shows_the_confirm_screen_and_does_not_start(self):
        self._check(info())
        start = self._start()
        q = FakeQuery("upgrade:ask:bbbbbbb")
        await ui.upgrade_action(q, bot=FakeBot())
        start.assert_not_awaited()
        self.assertIn("确认升级", q.message.texts[-1][0])

    async def test_local_changes_block_a_plain_upgrade_even_if_the_button_is_forged(self):
        self._check(info(dirty=["install.sh"]))
        start = self._start()
        await ui.upgrade_action(FakeQuery("upgrade:go:bbbbbbb"), bot=FakeBot())
        start.assert_not_awaited()

    async def test_reset_upgrade_goes_through_with_the_flag(self):
        self._check(info(dirty=["install.sh"]))
        start = self._start()
        with patch.object(upgrade, "finish_pending_upgrade", AsyncMock()):
            await ui.upgrade_action(FakeQuery("upgrade:go:bbbbbbb:reset"), bot=FakeBot())
        self.assertTrue(start.await_args.kwargs["reset"])

    async def test_already_up_to_date_does_not_upgrade(self):
        self._check(info(total=0, remote_sha=cur().sha))
        start = self._start()
        await ui.upgrade_action(FakeQuery("upgrade:go:bbbbbbb"), bot=FakeBot())
        start.assert_not_awaited()

    async def test_check_error_is_shown(self):
        self._check(UpgradeError("git fetch 失败"))
        q = FakeQuery("upgrade:go:bbbbbbb")
        await ui.upgrade_action(q, bot=FakeBot())
        self.assertIn("git fetch 失败", q.message.texts[-1][0])

    async def test_launch_failure_is_shown(self):
        self._check(info())
        self._start(side_effect=UpgradeError("启动升级任务失败"))
        q = FakeQuery("upgrade:go:bbbbbbb")
        await ui.upgrade_action(q, bot=FakeBot())
        self.assertIn("启动升级任务失败", q.message.texts[-1][0])

    async def test_unsupported_environment_explains_instead_of_checking(self):
        with patch.object(upgrade, "support", return_value=(False, "docker 模式请在服务器上运行：sudo tg-aria2 update")):
            q = FakeQuery("admin:upgrade")
            await ui.show_check(q)
        self.assertIn("sudo tg-aria2 update", q.message.texts[-1][0])

    async def test_callback_pattern_rejects_garbage(self):
        import re
        for bad in ("upgrade:go:", "upgrade:go:xyz", "upgrade:go:bbbbbbb;rm -rf /", "upgrade:foo:bbbbbbb", "upgrade:go:BBBBBBB"):
            self.assertIsNone(re.match(ui.ACTION_RE, bad), bad)
        self.assertTrue(re.match(ui.ACTION_RE, "upgrade:go:bbbbbbb:reset"))

    def test_whole_router_is_admin_only(self):
        for observer in (ui.router.message, ui.router.callback_query):
            self.assertTrue(any(isinstance(m, AdminMiddleware) for m in observer.middleware))

    def test_settings_menu_has_the_entry(self):
        from bot.core.keyboards import settings_keyboard
        self.assertIn("admin:upgrade", _cbs(settings_keyboard()))


class TestSettingsFile(unittest.TestCase):
    def test_job_file_roundtrip_is_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            orig = settings.db_path
            settings.db_path = os.path.join(d, "data", "tasks.db")
            try:
                upgrade.write_job({"a": 1})
                self.assertEqual(json.loads(upgrade.job_path().read_text()), {"a": 1})
                self.assertEqual(list(upgrade.job_path().parent.glob("*.tmp")), [])   # 没留下临时文件
                upgrade.clear_job()
                upgrade.clear_job()                                                    # 重复清除不报错
                self.assertIsNone(upgrade.read_job())
            finally:
                settings.db_path = orig


if __name__ == "__main__":
    unittest.main()
