"""Select a temporary database before any test imports application settings."""

import atexit
import os
import sys
import tempfile
from pathlib import Path

_work = tempfile.TemporaryDirectory(prefix="wb-isolated-tests-")
DATABASE_PATH = (Path(_work.name) / "test.db").resolve()
DATABASE_URL = "sqlite:///" + DATABASE_PATH.as_posix()

# unittest loads every named module before running setUp. Importing converter
# from an earlier suite used to freeze .env's real MySQL engine first.
if "admin.config" in sys.modules:
    raise RuntimeError(
        "Refusing tests: application settings were loaded before database isolation. Run tests in a fresh process."
    )
os.environ.update(ADMIN_DATABASE_URL=DATABASE_URL, ADMIN_REDIS_URL="", ADMIN_GATEWAY_LIMIT_BACKEND="memory")


def _cleanup():
    module = sys.modules.get("admin.db")
    engine = getattr(module, "engine", None)
    if engine is not None:
        assert_test_database(engine)
        engine.dispose()
    _work.cleanup()


atexit.register(_cleanup)


def assert_test_database(engine):
    if engine.dialect.name != "sqlite" or Path(engine.url.database).resolve() != DATABASE_PATH:
        raise RuntimeError("Refusing test database writes: engine is not the isolated temporary SQLite database")


async def collect_response(response, on_chunk=None):
    """Execute the ASGI response, including deferred stream preflight/cleanup."""
    import asyncio

    chunks = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            if on_chunk is not None:
                on_chunk(message["body"])

    await response({"type": "http", "method": "POST", "path": "/", "asgi": {"spec_version": "2.3"}}, receive, send)
    return b"".join(chunks)


def ensure_schema():
    """按生产一致的方式建表 + 迁移到全局测试库。

    1.14.0 起建表从模块导入挪到了 server 启动时的 init_db()；而 routing_policy /
    upstream_dispatch / security.recheck_key 会直接用 admin.db.SessionLocal 读
    system_settings 等表。不预先建表，任何走这条路径的用例都会报 no such table。
    """
    from admin.db import engine as _engine
    from admin.db import init_db

    assert_test_database(_engine)
    init_db()


ensure_schema()
