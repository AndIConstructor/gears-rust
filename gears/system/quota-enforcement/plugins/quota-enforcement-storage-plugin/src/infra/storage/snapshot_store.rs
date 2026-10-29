//! The two snapshot reads: `read_quota_snapshot` for one applicable set (the
//! read evaluate-preview runs) and the paginated `bulk_read_quota_snapshot`.
//!
//! # One transaction per read
//!
//! A read selects its Quota rows and assembles their snapshots in one
//! transaction, in that order:
//!
//! 1. The Quota rows, under the caller's scope. On `PostgreSQL` the
//!    transaction is `REPEATABLE READ`, so this first statement fixes the
//!    snapshot every later read sees: a Quota row, its counter row and the
//!    expired holds that correct the counter all come from one state.
//! 2. One clock reading, shared by every Quota on the page.
//! 3. The snapshots. A consumption Quota whose current window has no row yet
//!    reads that window as zero; the next mutating operation creates the row.
//!
//! A read writes nothing and takes no row lock (I3), so it never waits on a
//! writer and a writer never waits on it. On `PostgreSQL` the transaction is
//! also `READ ONLY`, so a write on this path fails rather than slipping in.

use quota_enforcement_sdk::{
    ApplicableQuotas, PageRequest, PageResult, QuotaId, QuotaSnapshot, QuotaType, StorageError,
};
use time::OffsetDateTime;
use toolkit_db::secure::{DBRunner, TxAccessMode, TxConfig, TxIsolationLevel};
use toolkit_security::AccessScope;

use super::consumption_store::{
    LockedCounter, SqlConsumptionStore, TxError, lift, snapshot_of_locked,
};
use super::cursor;
use super::entity::quota;
use super::quota_mapping;
use super::repo::consumption_counter_repo as counter_repo;
use super::repo::quota_repo::{self, SnapshotPair};

/// A page the transaction read: its snapshots, in `quota_id` order, and
/// whether more rows follow.
struct Page {
    snapshots: Vec<QuotaSnapshot>,
    more: bool,
}

// @cpt-flow:cpt-cf-quota-enforcement-flow-snapshot-read:p1
// @cpt-algo:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1
// @cpt-dod:cpt-cf-quota-enforcement-dod-snapshot-read-only:p1
impl SqlConsumptionStore {
    /// Every active Quota whose subject is in `applicable`, in `quota_id`
    /// order.
    ///
    /// # Errors
    ///
    /// As the contract documents for `read_quota_snapshot`.
    pub async fn read_quota_snapshot_impl(
        &self,
        scope: &AccessScope,
        applicable: &ApplicableQuotas,
    ) -> Result<Vec<QuotaSnapshot>, StorageError> {
        const OPERATION: &str = "read quota snapshot";
        let page = self
            .read_page(scope, vec![pair_of(applicable)], None, None)
            .await
            .map_err(|error| lift(OPERATION, error))?;
        Ok(page.snapshots)
    }

    /// One page of the Quotas `pairs` select, each once, after the cursor.
    ///
    /// # Errors
    ///
    /// As the contract documents for `bulk_read_quota_snapshot`.
    pub async fn bulk_read_quota_snapshot_impl(
        &self,
        scope: &AccessScope,
        pairs: &[ApplicableQuotas],
        page: PageRequest,
    ) -> Result<PageResult<QuotaSnapshot>, StorageError> {
        const OPERATION: &str = "bulk read quota snapshot";
        // @cpt-begin:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-resume
        let after = page
            .cursor
            .as_deref()
            .map(|cursor| cursor::decode(cursor).map_err(|_| StorageError::InvalidCursor))
            .transpose()?;
        // @cpt-end:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-resume
        let limit = cursor::effective_limit(page.limit);
        // @cpt-begin:cpt-cf-quota-enforcement-flow-snapshot-read:p1:inst-snp-page
        let read = self
            .read_page(
                scope,
                pairs.iter().map(pair_of).collect(),
                after.map(QuotaId::as_uuid),
                Some(limit),
            )
            .await
            .map_err(|error| lift(OPERATION, error))?;
        // @cpt-end:cpt-cf-quota-enforcement-flow-snapshot-read:p1:inst-snp-page
        // @cpt-begin:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-limit-if
        // @cpt-begin:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-limit
        // @cpt-begin:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-uniform
        // The cursor holds only the last `quota_id` returned; a page that
        // fits carries none.
        let next_cursor = if read.more {
            read.snapshots
                .last()
                .map(|snapshot| cursor::encode(snapshot.quota_id))
        } else {
            None
        };
        // @cpt-end:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-uniform
        // @cpt-end:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-limit
        // @cpt-end:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-limit-if
        // @cpt-begin:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-return
        Ok(PageResult {
            items: read.snapshots,
            next_cursor,
        })
        // @cpt-end:cpt-cf-quota-enforcement-algo-snapshot-pagination:p1:inst-spg-return
    }

    /// Run the read's one transaction.
    async fn read_page(
        &self,
        scope: &AccessScope,
        pairs: Vec<SnapshotPair>,
        after: Option<uuid::Uuid>,
        limit: Option<u32>,
    ) -> Result<Page, TxError> {
        let clock = std::sync::Arc::clone(&self.clock);
        let scope = scope.clone();
        self.db
            .transaction_ref_mapped_with_config(
                TxConfig {
                    isolation: Some(TxIsolationLevel::RepeatableRead),
                    access_mode: Some(TxAccessMode::ReadOnly),
                },
                move |tx| {
                    Box::pin(async move {
                        read_in_tx(tx, &scope, &pairs, after, limit, || clock()).await
                    })
                },
            )
            .await
    }
}

/// The storage filter of one applicable set.
fn pair_of(applicable: &ApplicableQuotas) -> SnapshotPair {
    SnapshotPair {
        tenant_id: applicable.tenant_id.as_uuid(),
        metric: applicable.metric.as_str().to_owned(),
        subjects: applicable
            .subjects
            .iter()
            .map(|subject| {
                (
                    subject.projection_type.to_string(),
                    subject.subject_id.clone(),
                )
            })
            .collect(),
    }
}

/// One attempt: select, read the clock, assemble.
async fn read_in_tx(
    tx: &impl DBRunner,
    scope: &AccessScope,
    pairs: &[SnapshotPair],
    after: Option<uuid::Uuid>,
    limit: Option<u32>,
    now_of: impl FnOnce() -> OffsetDateTime,
) -> Result<Page, TxError> {
    // One row past the page says whether another page follows.
    let mut rows = quota_repo::find_snapshot_rows(
        tx,
        scope,
        pairs,
        after,
        limit.map(|limit| u64::from(limit) + 1),
    )
    .await?;
    let more = limit.is_some_and(|limit| rows.len() > limit as usize);
    if let Some(limit) = limit {
        rows.truncate(limit as usize);
    }
    // Read after the page query, so the snapshot is fixed first.
    let now = now_of();
    let mut snapshots = Vec::with_capacity(rows.len());
    for row in &rows {
        // @cpt-begin:cpt-cf-quota-enforcement-flow-snapshot-read:p1:inst-snp-assemble
        snapshots.push(assemble(tx, scope, row, now).await?);
        // @cpt-end:cpt-cf-quota-enforcement-flow-snapshot-read:p1:inst-snp-assemble
    }
    Ok(Page { snapshots, more })
}

/// One Quota's snapshot.
async fn assemble(
    tx: &impl DBRunner,
    scope: &AccessScope,
    row: &quota::Model,
    now: OffsetDateTime,
) -> Result<QuotaSnapshot, TxError> {
    let quota = quota_mapping::row_to_quota(row.clone())?;
    let counter = if quota.quota_type == QuotaType::Consumption {
        let latest = counter_repo::find_latest(tx, scope, quota.id.as_uuid()).await?;
        LockedCounter::consumption(&quota, latest, now)
    } else {
        let row = counter_repo::find_allocation(tx, scope, quota.id.as_uuid()).await?;
        LockedCounter::allocation(row.map(|row| row.in_flight))
    };
    snapshot_of_locked(tx, scope, &quota, &counter, now).await
}

#[cfg(test)]
#[cfg_attr(coverage_nightly, coverage(off))]
#[path = "snapshot_store_tests.rs"]
mod tests;
