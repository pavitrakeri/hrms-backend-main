from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def _strip_leading_sql_comments(block: str) -> str:
    """
    Remove full-line SQL comments (-- ...) so a chunk is not skipped when it
    starts with a comment line followed by CREATE TABLE (split-by-; bug).
    """
    lines = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


async def run_migrations(conn):
    """Apply SQL migrations from /migrations (idempotent)."""
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    if not migrations_dir.exists():
        logger.warning("Migrations directory not found: %s", migrations_dir)
        return

    for sql_file in sorted(migrations_dir.glob("*.sql")):
        sql = sql_file.read_text(encoding="utf-8")
        statements = []
        for raw in sql.split(";"):
            stmt = _strip_leading_sql_comments(raw)
            if stmt:
                statements.append(stmt)
        for statement in statements:
            try:
                await conn.execute(statement)
            except Exception as exc:
                msg = str(exc).lower()
                if "already exists" in msg or "duplicate" in msg:
                    continue
                logger.error("Migration failed (%s): %s", sql_file.name, exc)
                raise
        logger.info("Applied migration: %s", sql_file.name)
