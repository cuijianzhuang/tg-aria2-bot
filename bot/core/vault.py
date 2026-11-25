"""共享 Fernet 加解密：视频 Cookie 与节点 RPC 密钥共用同一把数据密钥。

密钥文件在 <数据目录>/video-private/key（目录名是历史遗留——扫码登录先
用的它；现在节点密钥也靠同一把钥匙，所以备份时必须连这个 key 一起备份，
丢了 key 等于库里的 Cookie 和节点密钥都解不开，只能重新扫码/重新加节点）。

文本密文带 ``enc:v1:`` 前缀存进 TEXT 列，便于与存量明文区分：读到没有
前缀的值按明文旧值用，写路径一律落密文。

cryptography 只在可选视频组件线里（requirements-video.txt），所以这里
**懒加载**：主程序（bare 无视频组件时）没有这个库也能正常 import 和
运行——此时节点密钥退回明文存储并打一条警告（与本模块引入前行为一致，
不算回归），装上视频组件/Docker（lock 全量安装）后启动迁移会自动把
明文转成密文。视频 Cookie 路径本来就只在装了该库时可用，不受影响。
"""
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_PREFIX = "enc:v1:"
_warned_unavailable = False


class FernetVault:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "video-private"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def cipher(self):
        from cryptography.fernet import Fernet

        path = self.root / "key"
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                Path(temporary).unlink(missing_ok=True)
        return Fernet(path.read_bytes())

    # ---- bytes（视频 Cookie 的 JSON blob 走这条） ----

    def encrypt_bytes(self, data: bytes) -> bytes:
        return self.cipher().encrypt(data)

    def decrypt_bytes(self, blob: bytes) -> bytes:
        return self.cipher().decrypt(blob)

    # ---- text（节点 RPC 密钥等 TEXT 列走这条） ----

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return value.startswith(TEXT_PREFIX)

    def encrypt_text(self, plaintext: str) -> str:
        global _warned_unavailable
        if not plaintext or self.is_encrypted(plaintext):
            return plaintext
        try:
            cipher = self.cipher()
        except ImportError:
            if not _warned_unavailable:
                _warned_unavailable = True
                log.warning(
                    "cryptography is not installed (video component absent); "
                    "node secrets stay in plaintext until it is installed"
                )
            return plaintext
        return TEXT_PREFIX + cipher.encrypt(plaintext.encode()).decode()

    def decrypt_text(self, value: str) -> str:
        """密文解密；存量明文（无前缀）原样返回。密文解不开（key 丢失/被换、
        或 cryptography 未安装）时记一条错误并原样返回密文——节点会以认证
        失败的形式显形，不让整个启动流程陪葬，用户重新添加节点即可恢复。"""
        if not self.is_encrypted(value):
            return value
        try:
            from cryptography.fernet import InvalidToken

            return self.cipher().decrypt(value[len(TEXT_PREFIX):].encode()).decode()
        except ImportError:
            log.error("vault: cannot decrypt stored secret (cryptography not installed)")
            return value
        except InvalidToken:
            log.error("vault: cannot decrypt stored secret (key file missing or changed?)")
            return value
