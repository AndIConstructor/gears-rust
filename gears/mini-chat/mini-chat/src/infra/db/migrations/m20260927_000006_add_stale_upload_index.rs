use sea_orm_migration::prelude::*;
use sea_orm_migration::sea_orm::ConnectionTrait;

/// Index for the upload reaper scan (`status IN (pending, uploaded)`
/// ordered by `updated_at`). Not a partial index: the query binds the status
/// values as parameters, and `SQLite` uses a partial index only when the
/// query repeats its `WHERE` literally.
#[derive(DeriveMigrationName)]
pub struct Migration;

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager
            .get_connection()
            .execute_unprepared(
                "CREATE INDEX IF NOT EXISTS idx_attachments_stale_upload \
                 ON attachments (status, updated_at)",
            )
            .await?;
        Ok(())
    }

    async fn down(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager
            .get_connection()
            .execute_unprepared("DROP INDEX IF EXISTS idx_attachments_stale_upload")
            .await?;
        Ok(())
    }
}
