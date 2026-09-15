#!/usr/bin/env python
"""生成 AM_TOKEN(≥32 字符,url-safe)。用法:python scripts/gen_token.py"""

import secrets
import sys


def main() -> int:
    token = secrets.token_urlsafe(36)  # ≥32 字符
    print(token)
    print(
        "\n将上面一行填入 deploy/.env 的 AM_TOKEN=,并保管好;"
        "丢失则重新生成并更新 .env 后 docker compose up -d 重建。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
