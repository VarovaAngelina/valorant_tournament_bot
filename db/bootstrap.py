from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ensure_project_settings_table(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS project_settings (
                id INT PRIMARY KEY,
                rules_text TEXT NULL,
                rules_url VARCHAR(512) NULL,
                updated_at DATETIME NULL
            )
            """
        )
    )
    column_exists = (
        await db_session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'project_settings'
                  AND COLUMN_NAME = 'rules_url'
                """
            )
        )
    ).scalar_one()
    if not column_exists:
        await db_session.execute(
            text("ALTER TABLE project_settings ADD COLUMN rules_url VARCHAR(512) NULL")
        )
    await db_session.commit()
