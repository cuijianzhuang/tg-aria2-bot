#!/usr/bin/env python3
"""签发 / 检查 Pro 授权码（发行方使用，不要部署到用户的服务器上）。

    python tools/license.py keygen [--out license-private.key]
        生成一对 Ed25519 密钥：私钥写到文件（权限 600），公钥打印出来——把公钥填进
        bot/core/license.py 的 PUBLIC_KEY_B64 再发布。私钥丢了就没法再签发，泄露了谁都能
        给自己签授权，务必离线备份、不要提交进仓库（.gitignore 已排除 *.key）。

    python tools/license.py issue --key license-private.key --bot-id 123456789 \\
        --name "张三" --days 365 --max-vip 50
        签发授权码。--bot-id 用 '*' 表示不绑定机器人；--days 0 = 永久；--max-vip 0 = 不限。

    python tools/license.py inspect TOKEN
        不校验签名，只解开看里面的内容。
"""
import argparse
import base64
import json
import os
import sys
import time


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def keygen(args):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if os.path.exists(args.out):
        sys.exit(f"{args.out} 已存在，不覆盖。换个文件名，或确认不要了再手动删除。")
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(b64e(raw) + "\n")
    print(f"私钥已写入 {args.out}（权限 600）——离线备份，不要提交进仓库。")
    print("公钥（填进 bot/core/license.py 的 PUBLIC_KEY_B64）：")
    print(b64e(pub))


def issue(args):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if args.bot_id != "*" and not args.bot_id.isdigit():
        sys.exit("--bot-id 必须是机器人 token 冒号前面的数字，或 '*'")
    if args.days < 0 or args.max_vip < 0:
        sys.exit("--days / --max-vip 不能是负数")
    with open(args.key, encoding="utf-8") as f:
        key = Ed25519PrivateKey.from_private_bytes(b64d(f.read().strip()))
    now = int(time.time())
    payload = {
        "v": 1, "name": args.name, "bot_id": args.bot_id, "iat": now,
        "exp": now + args.days * 86400 if args.days else 0, "max_vip": args.max_vip,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    print(f"TGA2.{b64e(raw)}.{b64e(key.sign(raw))}")


def inspect(args):
    parts = args.token.strip().split(".")
    if len(parts) != 3 or parts[0] != "TGA2":
        sys.exit("格式不对")
    data = json.loads(b64d(parts[1]))
    for k, v in data.items():
        shown = time.strftime("%F %T", time.localtime(v)) if k in ("iat", "exp") and v else v
        print(f"{k}: {shown}")
    print("（这里没有校验签名）")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", default="license-private.key")
    k.set_defaults(fn=keygen)
    i = sub.add_parser("issue")
    i.add_argument("--key", required=True)
    i.add_argument("--bot-id", required=True)
    i.add_argument("--name", required=True)
    i.add_argument("--days", type=int, default=365)
    i.add_argument("--max-vip", type=int, default=0)
    i.set_defaults(fn=issue)
    n = sub.add_parser("inspect")
    n.add_argument("token")
    n.set_defaults(fn=inspect)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
