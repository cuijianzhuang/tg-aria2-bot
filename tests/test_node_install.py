"""子节点安装脚本（tg-aria2.sh node-install）：参数校验、生成的配置、完整流程。

完整流程用替身命令（systemctl/useradd/apt/curl/chown/aria2c）跑真的脚本，路径用环境变量
指到临时目录——验证的是脚本本身的逻辑，不是真的 systemd。"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tg-aria2.sh"
BASH = shutil.which("bash")


def run(args, env=None, cwd=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=e, cwd=cwd, timeout=60)


class TestDryRun(unittest.TestCase):
    def test_generates_config_and_unit(self):
        r = run(["node-install", "--dry-run", "--secret", "s3cretvalue", "--dir", "/srv/dl", "--port", "6801", "--name", "box-1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        for line in ("dir=/srv/dl", "rpc-listen-port=6801", "rpc-secret=s3cretvalue", "rpc-listen-all=true",
                     "enable-rpc=true", "continue=true"):
            self.assertIn(line, out)
        self.assertIn("User=aria2node", out)
        self.assertIn("NoNewPrivileges=true", out)
        self.assertIn("/addnode box-1 http://HOST:6801/jsonrpc s3cretvalue /srv/dl", out)

    def test_random_secret_is_generated_and_long_enough(self):
        r = run(["node-install", "--dry-run"])
        secret = re.search(r"rpc-secret=(\S+)", r.stdout).group(1)
        self.assertGreaterEqual(len(secret), 16)
        self.assertNotEqual(secret, re.search(r"rpc-secret=(\S+)", run(["node-install", "--dry-run"]).stdout).group(1))

    def test_local_only_stops_listening_on_all_interfaces(self):
        r = run(["node-install", "--dry-run", "--local-only"])
        self.assertIn("rpc-listen-all=false", r.stdout)

    def test_invalid_arguments_are_rejected(self):
        for args in (["--port", "99999"], ["--port", "abc"], ["--dir", "relative/path"], ["--dir", "/a b"],
                     ["--dir", "/x;rm -rf /"], ["--name", "bad name"], ["--name", "a$(id)"], ["--secret", "short"],
                     ["--secret", "has space in it"], ["--allow-from", "1.2.3.4; reboot"], ["--bogus"]):
            r = run(["node-install", "--dry-run", *args])
            self.assertNotEqual(r.returncode, 0, args)
            self.assertNotIn("rpc-secret", r.stdout, args)

    def test_install_requires_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        self.assertNotEqual(run(["node-install"]).returncode, 0)

    def test_help_lists_node_commands(self):
        out = run(["--help"]).stdout
        for word in ("node-install", "node-info", "node-uninstall", "--allow-from"):
            self.assertIn(word, out)

    def test_menu_offers_the_node_entries(self):
        text = SCRIPT.read_text()
        for needle in ("15.${C_RESET} 安装子节点", "16.${C_RESET} 查看子节点信息", "17.${C_RESET} 卸载子节点",
                       "15) node_install_menu", 'node-info', 'node-uninstall'):
            self.assertIn(needle, text)

    def test_menu_install_passes_only_the_filled_in_answers(self):
        """菜单里全部回车 = 不带任何选项，走默认值；填了非法值会被 node-install 的校验拦下，什么都不会装。"""
        if not shutil.which("script"):
            self.skipTest("需要 script 命令来模拟终端")
        r = subprocess.run(
            ["script", "-qc", f"{BASH} {SCRIPT}", "/dev/null"], capture_output=True, text=True, timeout=60,
            input="15\n\n\n\nnot an ip!\n\n\n0\n", env={**os.environ, "TERM": "dumb"},
        )
        out = r.stdout
        self.assertIn("--allow-from 请填主机器人所在服务器的 IP", out)
        self.assertNotIn("子节点已就绪", out)

    def test_script_is_valid_bash(self):
        self.assertEqual(subprocess.run([BASH, "-n", str(SCRIPT)]).returncode, 0)


STUBS = {
    # 记录调用，什么都不真的做
    "systemctl": 'echo "systemctl $*" >> "$STUB_LOG"; [ "$1" = is-active ] && echo active; exit 0',
    "useradd": 'echo "useradd $*" >> "$STUB_LOG"',
    "id": 'exit 1',
    "chown": 'echo "chown $*" >> "$STUB_LOG"',
    "journalctl": 'exit 0',
    "ufw": 'echo "ufw $*" >> "$STUB_LOG"; [ "$1" = status ] && echo "Status: active"; exit 0',
    "aria2c": 'echo "aria2c version 1.37.0"',
    "sleep": 'exit 0',
    # RPC 探活返回版本号；公网 IP 探测失败
    "curl": 'case "$*" in *jsonrpc*) echo \'{"result":{"version":"1.37.0"}}\' ;; *) exit 22 ;; esac',
    "hostname": '[ "$1" = -s ] && echo testbox || echo 10.9.8.7',
}


@unittest.skipUnless(os.geteuid() == 0, "完整流程里脚本会检查 root")
class TestFullFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        bindir = t / "bin"
        bindir.mkdir()
        for name, body in STUBS.items():
            f = bindir / name
            f.write_text(f"#!/bin/sh\n{body}\n")
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        self.log = t / "calls.log"
        self.env = {
            "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_LOG": str(self.log),
            "NODE_CONF_DIR": str(t / "etc"), "NODE_STATE_DIR": str(t / "state"),
            "NODE_UNIT_FILE": str(t / "unit.service"),
        }
        self.dl = str(t / "downloads")
        self.t = t

    def _install(self, *extra):
        return run(["node-install", "--dir", self.dl, "--host", "203.0.113.5", "--name", "tokyo", *extra], env=self.env)

    def test_install_creates_everything_and_prints_the_addnode_command(self):
        r = self._install("--allow-from", "198.51.100.7")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        conf = (self.t / "etc" / "aria2.conf").read_text()
        secret = re.search(r"rpc-secret=(\S+)", conf).group(1)
        self.assertRegex(r.stdout, rf"/addnode tokyo http://203\.0\.113\.5:6800/jsonrpc {secret} {re.escape(self.dl)}")
        self.assertTrue(os.path.isdir(self.dl))
        self.assertEqual(stat.S_IMODE((self.t / "etc" / "aria2.conf").stat().st_mode), 0o640)   # 里面有密钥
        self.assertEqual(stat.S_IMODE((self.t / "etc" / "node.info").stat().st_mode), 0o600)
        unit = (self.t / "unit.service").read_text()
        self.assertIn(f"--conf-path={self.t / 'etc' / 'aria2.conf'}", unit)
        calls = self.log.read_text()
        self.assertIn("systemctl enable tg-aria2-node", calls)
        self.assertIn("systemctl restart tg-aria2-node", calls)
        self.assertIn("useradd --system", calls)
        self.assertIn("ufw allow from 198.51.100.7 to any port 6800 proto tcp", calls)

    def test_rerun_keeps_the_existing_secret(self):
        first = self._install()
        second = self._install()
        s1 = re.search(r"/addnode \S+ \S+ (\S+) ", first.stdout).group(1)
        s2 = re.search(r"/addnode \S+ \S+ (\S+) ", second.stdout).group(1)
        self.assertEqual(s1, s2)

    def test_explicit_secret_replaces_the_old_one(self):
        self._install()
        r = self._install("--secret", "brand-new-secret")
        self.assertIn("brand-new-secret", r.stdout)

    def test_without_allow_from_it_warns_about_the_open_port(self):
        r = self._install()
        self.assertIn("没有限制来源", r.stdout + r.stderr)
        self.assertNotIn("ufw allow", self.log.read_text())

    def test_node_info_reprints_the_command(self):
        first = self._install()
        info = run(["node-info"], env=self.env)
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertEqual(re.search(r"/addnode .*", first.stdout).group(0), re.search(r"/addnode .*", info.stdout).group(0))

    def test_node_info_without_install(self):
        self.assertNotEqual(run(["node-info"], env=self.env).returncode, 0)

    def test_uninstall_keeps_config_unless_purged(self):
        self._install()
        r = run(["node-uninstall", "-y"], env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.t / "unit.service").exists())
        self.assertTrue((self.t / "etc" / "aria2.conf").exists())
        self.assertTrue(os.path.isdir(self.dl))                    # 下载的文件永远不动
        run(["node-uninstall", "-y", "--purge"], env=self.env)
        self.assertFalse((self.t / "etc").exists())
        self.assertTrue(os.path.isdir(self.dl))

    def test_rpc_never_answering_fails_loudly(self):
        (self.t / "bin" / "curl").write_text("#!/bin/sh\nexit 22\n")
        r = self._install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("RPC", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
