import os
import sqlite3
import tempfile
import unittest

from bot.core.node_pool import NodePool
from bot.core.vault import FernetVault
from bot.db.repo import TaskRepo
from bot.video.accounts import CredentialVault


class TestFernetVaultText(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.vault = FernetVault(self._dir.name)

    def test_text_roundtrip(self):
        stored = self.vault.encrypt_text("rpc-secret-123")
        self.assertTrue(stored.startswith("enc:v1:"))
        self.assertNotIn("rpc-secret-123", stored)
        self.assertEqual(self.vault.decrypt_text(stored), "rpc-secret-123")

    def test_legacy_plaintext_passthrough(self):
        self.assertEqual(self.vault.decrypt_text("plain-old-secret"), "plain-old-secret")
        self.assertFalse(FernetVault.is_encrypted("plain-old-secret"))

    def test_encrypt_is_idempotent_and_empty_safe(self):
        once = self.vault.encrypt_text("abc")
        self.assertEqual(self.vault.encrypt_text(once), once)
        self.assertEqual(self.vault.encrypt_text(""), "")

    def test_missing_cryptography_degrades_to_plaintext(self):
        # bare 无视频组件的部署没有 cryptography：加密退回明文（有警告日志），
        # 解密遇到密文也原样返回，都不能抛 ImportError 把主流程带崩。
        from unittest.mock import patch

        with patch.object(FernetVault, "cipher", side_effect=ImportError("no cryptography")):
            self.assertEqual(self.vault.encrypt_text("plain"), "plain")
            stored = "enc:v1:gAAAAABfake"
            self.assertEqual(self.vault.decrypt_text(stored), stored)

    def test_shares_key_with_video_credential_vault(self):
        # 节点密钥与视频 Cookie 共用同一把 key：用 CredentialVault 加密的
        # 密文，FernetVault 必须能解（反之亦然），备份只需带一份 key。
        cookies_vault = CredentialVault(self._dir.name)
        blob = cookies_vault.encrypt([{"name": "a", "value": "b", "domain": ".x.com"}])
        self.assertEqual(cookies_vault.decrypt(blob)[0]["name"], "a")
        stored = self.vault.encrypt_text("shared-key-check")
        other = FernetVault(self._dir.name)
        self.assertEqual(other.decrypt_text(stored), "shared-key-check")


class NodeSecretRepoCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._dir.name, "t.db")
        self.repo = TaskRepo(self.db_path)
        await self.repo.connect()

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    def _raw_secret(self, name: str) -> str:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT secret FROM nodes WHERE name = ?", (name,)).fetchone()
            return row[0]
        finally:
            conn.close()


class TestNodeSecretStorage(NodeSecretRepoCase):
    async def test_add_node_stores_ciphertext_and_reads_back_plaintext(self):
        await self.repo.add_node(
            name="nas", rpc_url="http://nas:6800/jsonrpc", secret="top-secret",
            download_dir="/volume1/downloads",
        )
        raw = self._raw_secret("nas")
        self.assertTrue(raw.startswith("enc:v1:"), raw)
        self.assertNotIn("top-secret", raw)
        rows = await self.repo.list_nodes()
        self.assertEqual(rows[0]["secret"], "top-secret")

    async def test_pool_load_gets_plaintext_secret(self):
        await self.repo.add_node(
            name="nas", rpc_url="http://nas:6800/jsonrpc", secret="pool-secret",
            download_dir="/volume1/downloads",
        )
        pool = NodePool(self.repo)
        await pool.load()
        self.assertEqual(pool.get_node("nas").secret, "pool-secret")


class TestLegacyPlaintextMigration(NodeSecretRepoCase):
    async def test_plaintext_row_migrated_on_connect(self):
        # 模拟存量库：直接写一条明文 secret，再重连触发启动迁移
        await self.repo._conn.execute(
            "INSERT INTO nodes (name, rpc_url, secret, download_dir, is_local, enabled)"
            " VALUES (?, ?, ?, ?, 0, 1)",
            ("old", "http://old:6800/jsonrpc", "legacy-plain", "/d"),
        )
        await self.repo._conn.commit()
        await self.repo.close()

        repo2 = TaskRepo(self.db_path)
        await repo2.connect()
        try:
            raw_conn = sqlite3.connect(self.db_path)
            try:
                raw = raw_conn.execute("SELECT secret FROM nodes WHERE name = 'old'").fetchone()[0]
            finally:
                raw_conn.close()
            self.assertTrue(raw.startswith("enc:v1:"), raw)
            rows = await repo2.list_nodes()
            self.assertEqual(rows[0]["secret"], "legacy-plain")
        finally:
            await repo2.close()

        # 再次启动：已是密文，不应被重复加密（解密结果不变）
        repo3 = TaskRepo(self.db_path)
        await repo3.connect()
        try:
            rows = await repo3.list_nodes()
            self.assertEqual(rows[0]["secret"], "legacy-plain")
        finally:
            await repo3.close()


if __name__ == "__main__":
    unittest.main()
