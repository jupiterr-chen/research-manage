"""uvicorn 入口:load settings → create_app(DESIGN §1)。用法:python -m app.main"""

from __future__ import annotations

import logging

import uvicorn

from app.config import Settings
from app.web.server import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.load()  # 校验失败 → SystemExit(2) 并打印原因
    app = create_app(settings)
    uvicorn.run(app, host=settings.bind, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
