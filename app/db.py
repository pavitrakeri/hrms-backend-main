import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
_db_pool = None  # internal variable

async def init_db():
    global _db_pool
    _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with _db_pool.acquire() as conn:
        from app.migrate import run_migrations
        await run_migrations(conn)

async def close_db():
    global _db_pool
    if _db_pool:
        await _db_pool.close()

def get_db_pool():
    if _db_pool is None:
        raise RuntimeError("Database pool not initialized")
    return _db_pool
