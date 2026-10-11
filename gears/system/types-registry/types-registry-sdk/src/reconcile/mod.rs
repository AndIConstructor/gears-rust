//! Reconcile explicit documents by reading current content and submitting differences.
//! Transport retries reuse the key and request; new passes re-read and use new keys.
//! Dependencies, conflicts and call refusals stay pending; item refusals are terminal.
//! Matching content does not confirm the stored publisher version.

use std::collections::{BTreeMap, HashMap};
use std::num::{NonZeroU32, NonZeroUsize};
use std::time::Duration;

use gts::GtsId;
use tokio::time::{Instant, sleep_until};
use tokio_util::sync::CancellationToken;
use toolkit_canonical_errors::{CanonicalError, InvalidArgument};
use toolkit_security::PlatformSecurityContext;
use tracing::Instrument as _;
use uuid::Uuid;

use crate::contract::PlatformTypesRegistryApi;
use crate::gts::TypeResource;
use crate::item_failure::{AdmissionFailure, AdmissionFailureReason as Reason};
use crate::models::{
    BatchGetEntitiesRequest, BatchGetItem, EntityField, EntityKey, EntityLookup, FieldSelection,
    IdempotencyKey, JsonDocument, LifecycleStatus, MAX_BATCH_GET_KEYS, Origin, Projection,
    PublisherContext, RegisterEntitiesRequest, RegisterItem, RegistrationOutcome,
};

pub mod submit;
use submit::{Stop, await_registration, bounded, deadline_after, jittered};

mod telemetry;
use telemetry::{Run, StopReason, error_fields, operation_id_of};

/// How a submitted batch is retried at the transport level, key and request unchanged.
const TRANSPORT_ATTEMPTS: u32 = 3;

/// Initial transport retry pause; doubles with jitter under the call deadline.
const TRANSPORT_RETRY_BACKOFF: Duration = Duration::from_millis(100);

/// Reconciliation tuning; initialize with Default, then adjust fields.
#[derive(Debug, Clone)]
#[non_exhaustive]
pub struct ReconcileOptions {
    /// Candidates per submission. A synchronous refusal naming one candidate rejects it and
    /// resubmits the rest; a refusal of the batch's size is bisected; a refusal of the request
    /// as a whole is submitted once and leaves the batch pending.
    pub batch_size: NonZeroUsize,
    /// Read–compare–submit passes in one call.
    pub passes: NonZeroU32,
    /// Initial pass backoff; clamp to [`Self::retry_backoff_max`], then double with jitter.
    pub retry_backoff: Duration,
    /// Longest pause between two passes.
    pub retry_backoff_max: Duration,
    /// Budget of the whole call, every pass and poll included.
    pub deadline: Duration,
}

impl Default for ReconcileOptions {
    fn default() -> Self {
        Self {
            batch_size: NonZeroUsize::new(100).unwrap_or(NonZeroUsize::MIN),
            passes: NonZeroU32::new(5).unwrap_or(NonZeroU32::MIN),
            retry_backoff: Duration::from_millis(200),
            retry_backoff_max: Duration::from_secs(5),
            deadline: Duration::from_secs(60),
        }
    }
}

/// What one call achieved.
#[derive(Debug, Clone)]
pub enum Reconciliation {
    /// Every desired document already matched; nothing was submitted.
    UpToDate,
    /// Every desired identifier's outcome, when work was submitted or remains undecided.
    Reconciled(HashMap<GtsId, ReconcileOutcome>),
}

/// One desired identifier's outcome.
#[derive(Debug, Clone)]
pub enum ReconcileOutcome {
    /// Registered, or already present with the desired content.
    Admitted,
    /// Terminal refusal; decode registry reasons with [`AdmissionFailure::from_canonical`].
    Rejected(CanonicalError),
    /// Not settled by this call.
    Pending(ReconcilePendingCause),
}

/// Why an identifier is still pending.
#[derive(Debug, Clone)]
pub enum ReconcilePendingCause {
    /// Something it depends on is not registered yet.
    Dependency(CanonicalError),
    /// A concurrent writer changed it; the next pass re-reads.
    Conflict(CanonicalError),
    /// The registry could not be reached or did not answer in time.
    Unavailable(CanonicalError),
    /// Call-level refusal (e.g. auth/permission); this call does not submit it again, a
    /// later one may once its cause is fixed.
    Refused(CanonicalError),
}

impl ReconcilePendingCause {
    /// Transport/availability failure maps to Unavailable; other call failures to Refused.
    pub(crate) fn of(error: CanonicalError) -> Self {
        if transport_failure(&error) {
            Self::Unavailable(error)
        } else {
            Self::Refused(error)
        }
    }
}

impl ReconcileOutcome {
    /// Whether another call could change this outcome.
    #[must_use]
    pub fn is_settled(&self) -> bool {
        !matches!(self, Self::Pending(_))
    }
}

/// Reconcile desired documents under `publisher`; callers reach it through
/// `PlatformTypesRegistryApiExt::reconcile_entities_and_await`, which documents it.
///
/// The call runs in an INFO `reconcile` span; each submitted payload in an INFO
/// `reconcile_batch` span within it (see the README's Observability section).
pub async fn reconcile<'d, A, I>(
    api: &A,
    ctx: &PlatformSecurityContext,
    publisher: &PublisherContext,
    desired: I,
    options: &ReconcileOptions,
    cancel: &CancellationToken,
) -> Result<Reconciliation, CanonicalError>
where
    A: PlatformTypesRegistryApi + ?Sized,
    I: IntoIterator<Item = &'d (GtsId, JsonDocument)>,
{
    // The whole-call budget starts now, before any preparation.
    let started = Instant::now();
    let deadline = deadline_after(started, options.deadline);
    let span = tracing::info_span!(
        "reconcile",
        reconcile_id = %Uuid::new_v4(),
        publisher = %publisher.name,
        publisher_version = %publisher.version,
        inputs = tracing::field::Empty,
        desired = tracing::field::Empty,
        passes = options.passes.get(),
        batch_size = options.batch_size.get(),
        deadline_ms = u64::try_from(options.deadline.as_millis()).unwrap_or(u64::MAX),
    );
    let (wanted, outcomes) = span.in_scope(|| validate(desired));
    let prepared = Prepared {
        started,
        deadline,
        wanted,
        outcomes,
    };
    run(api, ctx, publisher, cancel, options, prepared)
        .instrument(span)
        .await
}

/// A call's input, validated, and its budget.
struct Prepared<'d> {
    started: Instant,
    deadline: Result<Instant, CanonicalError>,
    wanted: Wanted<'d>,
    outcomes: Outcomes,
}

async fn run<A: PlatformTypesRegistryApi + ?Sized>(
    api: &A,
    ctx: &PlatformSecurityContext,
    publisher: &PublisherContext,
    cancel: &CancellationToken,
    options: &ReconcileOptions,
    prepared: Prepared<'_>,
) -> Result<Reconciliation, CanonicalError> {
    // The guard reports the call's end, whichever way it ends, dropped included.
    let desired = prepared.wanted.len() + prepared.outcomes.len();
    let mut run = Run::new(prepared.started, desired, prepared.outcomes);
    let mut wanted = prepared.wanted;
    tracing::info!(event = "reconcile_started", "reconciliation started");
    let deadline = match prepared.deadline {
        Ok(deadline) => deadline,
        Err(error) => {
            run.stop(StopReason::InvalidDeadline);
            return Err(error);
        }
    };
    let mut call = Call {
        api,
        ctx,
        publisher,
        deadline,
        cancel,
        batches: 0,
    };
    let mut backoff = options.retry_backoff.min(options.retry_backoff_max);
    let mut stop = StopReason::PassesExhausted;
    for pass in 1..=options.passes.get() {
        run.passes = pass;
        let end = if pass > 1
            && let Err(stopped) = pause(jittered(backoff), deadline, cancel).await
        {
            stopped_between_passes(stopped, &wanted, &mut run)
        } else {
            call.pass(pass, options.batch_size.get(), &mut wanted, &mut run)
                .await
        };
        // Each later pause doubles, up to the maximum.
        if pass > 1 {
            backoff = backoff.saturating_mul(2).min(options.retry_backoff_max);
        }
        match end {
            PassEnd::Next => {}
            PassEnd::Stop(reason) => {
                stop = reason;
                break;
            }
            PassEnd::Cancelled => {
                run.stop(StopReason::Cancelled);
                return Err(Stop::Cancelled.into_error());
            }
        }
    }
    // The last pass ran out of time rather than of passes: the local budget, not a remote
    // `DeadlineExceeded` answered while it still had some.
    if stop == StopReason::PassesExhausted && Instant::now() >= deadline {
        stop = StopReason::Deadline;
    }
    Ok(conclude(run, &wanted, call.batches > 0, stop))
}

/// The pause before a later pass, ended by the caller or the deadline.
fn stopped_between_passes(stopped: Stop, wanted: &Wanted<'_>, run: &mut Run) -> PassEnd {
    match stopped {
        Stop::Cancelled => PassEnd::Cancelled,
        Stop::Deadline(_) => {
            let cause = ReconcilePendingCause::Unavailable(stopped.into_error());
            mark_unsettled(wanted, &mut run.outcomes, &cause);
            PassEnd::Stop(StopReason::Deadline)
        }
    }
}

/// How one pass ended.
enum PassEnd {
    Next,
    Stop(StopReason),
    Cancelled,
}

/// Give every identifier still wanted an outcome, report the end, and answer.
fn conclude(
    mut run: Run,
    wanted: &Wanted<'_>,
    submitted: bool,
    stop: StopReason,
) -> Reconciliation {
    // Whatever stopped the passes, nothing desired leaves without an outcome.
    for (key, (id, _)) in wanted {
        run.outcomes.entry(key.clone()).or_insert_with(|| {
            (
                id.clone(),
                ReconcileOutcome::Pending(ReconcilePendingCause::Unavailable(
                    CanonicalError::internal(
                        "reconciliation ended before this identifier was decided",
                    )
                    .create(),
                )),
            )
        });
    }
    if !submitted
        && run
            .outcomes
            .values()
            .all(|(_, o)| matches!(o, ReconcileOutcome::Admitted))
    {
        run.finish(StopReason::UpToDate);
        return Reconciliation::UpToDate;
    }
    Reconciliation::Reconciled(run.finish(stop).into_values().collect())
}

fn millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

struct Call<'c, A: ?Sized> {
    api: &'c A,
    ctx: &'c PlatformSecurityContext,
    publisher: &'c PublisherContext,
    deadline: Instant,
    cancel: &'c CancellationToken,
    /// Payloads submitted so far; numbers each `reconcile_batch` span.
    batches: u32,
}

/// A pause of `backoff`, cut short by the deadline or the caller's cancellation.
async fn pause(
    backoff: Duration,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<(), Stop> {
    let wake = Instant::now()
        .checked_add(backoff)
        .map_or(deadline, |wake| wake.min(deadline));
    bounded(deadline, cancel, None, sleep_until(wake)).await
}

/// Settle active content matches immediately; return only remaining candidates, avoiding
/// resubmission/downgrade.
fn compare(
    wanted: &mut Wanted<'_>,
    outcomes: &mut Outcomes,
    current: &HashMap<String, Current>,
) -> Vec<RegisterItem> {
    let mut candidates = Vec::new();
    wanted.retain(|key, (id, content)| match current.get(key) {
        Some(found) if found.lifecycle == LifecycleStatus::Active && found.content == **content => {
            outcomes.insert(key.clone(), (id.clone(), ReconcileOutcome::Admitted));
            false
        }
        found => {
            candidates.push(RegisterItem {
                gts_id: id.clone(),
                content: (*content).clone(),
                expected_resource_version: found.map(|f| f.resource_version),
                force: false,
            });
            true
        }
    });
    candidates
}

/// Record decisions and remove the identifiers this call will not submit again.
fn record(
    decided: impl IntoIterator<Item = (GtsId, ReconcileOutcome)>,
    wanted: &mut Wanted<'_>,
    outcomes: &mut Outcomes,
) {
    for (id, outcome) in decided {
        let key = id.id().to_owned();
        // A refused call is not retried by another pass: re-reading does not fix its cause.
        // It stays pending, so a later call can.
        if outcome.is_settled()
            || matches!(
                outcome,
                ReconcileOutcome::Pending(ReconcilePendingCause::Refused(_))
            )
        {
            wanted.remove(&key);
        }
        outcomes.insert(key, (id, outcome));
    }
}

/// Desired documents not yet settled, by canonical identifier: submission order is stable.
type Wanted<'d> = BTreeMap<String, (GtsId, &'d JsonDocument)>;

/// Outcomes so far, by canonical identifier.
pub type Outcomes = BTreeMap<String, (GtsId, ReconcileOutcome)>;

struct Current {
    content: JsonDocument,
    resource_version: u64,
    lifecycle: LifecycleStatus,
}

/// Collapse identical duplicates and reject conflicting ones before transport. A `GtsId` is
/// valid and canonical by construction, so nothing else is checked here.
fn validate<'d>(
    desired: impl IntoIterator<Item = &'d (GtsId, JsonDocument)>,
) -> (Wanted<'d>, Outcomes) {
    let mut wanted: Wanted<'d> = BTreeMap::new();
    let mut outcomes = Outcomes::new();
    let mut conflicting = Vec::new();
    let mut inputs = 0_usize;
    for (id, content) in desired {
        inputs += 1;
        match wanted.get(id.id()) {
            Some((_, existing)) if *existing != content => conflicting.push(id.clone()),
            Some(_) => {}
            None => {
                wanted.insert(id.id().to_owned(), (id.clone(), content));
            }
        }
    }
    for id in conflicting {
        wanted.remove(id.id());
        let error = TypeResource::invalid_argument()
            .with_field_violation(
                crate::field::GTS_ID_FIELD,
                format!("'{id}' is declared twice with different documents"),
                crate::field::VALIDATION_FAILED,
            )
            .create();
        outcomes.insert(id.id().to_owned(), (id, ReconcileOutcome::Rejected(error)));
    }
    let span = tracing::Span::current();
    span.record("inputs", inputs);
    span.record("desired", wanted.len() + outcomes.len());
    (wanted, outcomes)
}

/// Every identifier still wanted is unsettled; record the latest cause.
fn mark_unsettled(wanted: &Wanted<'_>, outcomes: &mut Outcomes, cause: &ReconcilePendingCause) {
    for (key, (id, _)) in wanted {
        outcomes.insert(
            key.clone(),
            (id.clone(), ReconcileOutcome::Pending(cause.clone())),
        );
    }
}

impl<A: PlatformTypesRegistryApi + ?Sized> Call<'_, A> {
    /// One read–compare–submit pass; outcomes are recorded in `run` as they are decided.
    async fn pass(
        &mut self,
        pass: u32,
        batch_size: usize,
        wanted: &mut Wanted<'_>,
        run: &mut Run,
    ) -> PassEnd {
        if self.cancel.is_cancelled() {
            return PassEnd::Cancelled;
        }
        // Only the caller's token ends the call; a `Cancelled` from the registry is an answer.
        let current = match self.read(wanted.values().map(|(id, _)| id)).await {
            Ok(Ok(current)) => current,
            Err(Stop::Cancelled) => return PassEnd::Cancelled,
            Err(stop @ Stop::Deadline(_)) => {
                let cause = ReconcilePendingCause::of(stop.into_error());
                mark_unsettled(wanted, &mut run.outcomes, &cause);
                return PassEnd::Stop(StopReason::Deadline);
            }
            Ok(Err(e)) => {
                mark_unsettled(wanted, &mut run.outcomes, &ReconcilePendingCause::of(e));
                return PassEnd::Stop(StopReason::ReadFailed);
            }
        };
        let candidates = compare(wanted, &mut run.outcomes, &current);
        if candidates.is_empty() {
            wanted.clear();
            return PassEnd::Stop(StopReason::Completed);
        }
        if self
            .submit_all(candidates, batch_size, pass, wanted, run)
            .await
            .is_err()
        {
            return PassEnd::Cancelled;
        }
        if wanted.is_empty() {
            PassEnd::Stop(StopReason::Completed)
        } else {
            PassEnd::Next
        }
    }

    /// Reads `ids` in bounded batches, selecting `content` and `origin`. The outer `Err` is
    /// this call's own stop, the inner one the registry's failure or a protocol fault.
    async fn read<'a>(
        &self,
        ids: impl Iterator<Item = &'a GtsId>,
    ) -> Result<Result<HashMap<String, Current>, CanonicalError>, Stop> {
        let projection = Projection::Select(FieldSelection::with(&[
            EntityField::Content,
            EntityField::Origin,
        ]));
        let ids: Vec<&GtsId> = ids.collect();
        let mut current = HashMap::with_capacity(ids.len());
        for chunk in ids.chunks(MAX_BATCH_GET_KEYS) {
            let request = BatchGetEntitiesRequest {
                items: chunk
                    .iter()
                    .map(|id| BatchGetItem::from(EntityKey::GtsId((*id).clone())))
                    .collect(),
                projection: projection.clone(),
                fresh: true,
            };
            let mut lookups = match bounded(
                self.deadline,
                self.cancel,
                None,
                self.api.batch_get_entities(self.ctx, request),
            )
            .await?
            {
                Ok(lookups) => lookups,
                Err(error) => return Ok(Err(error)),
            };
            // Only explicit NotFound permits creation; missing, Unchanged, or incomplete Found is a
            // protocol fault.
            for id in chunk {
                let key = EntityKey::GtsId((*id).clone());
                match lookups.0.remove(&key) {
                    Some(EntityLookup::NotFound) => {}
                    Some(EntityLookup::Found {
                        entity: snapshot, ..
                    }) => {
                        let (
                            Some(Origin::Managed {
                                resource_version, ..
                            }),
                            Some(content),
                        ) = (snapshot.origin, snapshot.content)
                        else {
                            return Ok(Err(incomplete_read(
                                id,
                                "a found entity lacks a selected field",
                            )));
                        };
                        current.insert(
                            id.id().to_owned(),
                            Current {
                                content,
                                resource_version,
                                lifecycle: snapshot.lifecycle_status,
                            },
                        );
                    }
                    Some(EntityLookup::Unchanged { .. }) => {
                        return Ok(Err(incomplete_read(
                            id,
                            "an unconditional read answered unchanged",
                        )));
                    }
                    None => {
                        return Ok(Err(incomplete_read(id, "the read did not answer this key")));
                    }
                }
            }
        }
        Ok(Ok(current))
    }

    /// Submit bounded batches and record every candidate's outcome as its batch is decided,
    /// so a later stop loses nothing already observed. `Err` is the caller's cancellation.
    async fn submit_all(
        &mut self,
        candidates: Vec<RegisterItem>,
        batch_size: usize,
        pass: u32,
        wanted: &mut Wanted<'_>,
        run: &mut Run,
    ) -> Result<(), Stop> {
        // Each payload with the batch it came from: a refused remainder or a split half.
        let mut queue: Vec<(Option<u32>, Vec<RegisterItem>)> = Vec::new();
        let mut rest = candidates.into_iter().peekable();
        while rest.peek().is_some() {
            queue.push((None, rest.by_ref().take(batch_size).collect()));
        }
        queue.reverse();
        while let Some((parent_batch, batch)) = queue.pop() {
            let key = IdempotencyKey::generate();
            self.batches += 1;
            let batch_id = self.batches;
            let span = tracing::info_span!(
                "reconcile_batch",
                batch = batch_id,
                parent_batch,
                pass,
                size = batch.len(),
                idempotency_key = key.as_str(),
                attempt = tracing::field::Empty,
                operation_id = tracing::field::Empty,
            );
            let submitted = self.submit_batch(key, batch).instrument(span.clone()).await;
            // Synchronously in the batch's span: its refusal events carry its fields.
            let decided = span.in_scope(|| decide(submitted, batch_id, &mut queue))?;
            record(decided, wanted, &mut run.outcomes);
        }
        Ok(())
    }

    /// Submit one payload under `key`; transport retries preserve it and use jittered pauses
    /// under the deadline. Runs inside the batch's span.
    async fn submit_batch(&self, key: IdempotencyKey, items: Vec<RegisterItem>) -> Submitted {
        let request = RegisterEntitiesRequest {
            items,
            dry_run: false,
            publisher: self.publisher.clone(),
        };
        // Before anything is sent: the key reaches an INFO log even if no answer ever does.
        tracing::info!(event = "batch_submitted", "submitting a batch");
        let mut backoff = TRANSPORT_RETRY_BACKOFF;
        let mut attempt = 0;
        loop {
            attempt += 1;
            tracing::Span::current().record("attempt", attempt);
            let error = match await_registration(
                self.api,
                self.ctx,
                key.clone(),
                request.clone(),
                self.deadline,
                self.cancel,
            )
            .await
            {
                Ok(Ok(operation)) => {
                    return Submitted::Decided(cover(&request.items, operation.items));
                }
                Ok(Err(e @ CanonicalError::InvalidArgument { .. })) => {
                    return Submitted::Refused(e, request.items);
                }
                // A registry's `Cancelled` lands here: not retried, the batch stays pending.
                Ok(Err(e)) => e,
                Err(Stop::Cancelled) => return Submitted::Cancelled,
                Err(stop @ Stop::Deadline(_)) => stop.into_error(),
            };
            if let Some(operation_id) = operation_id_of(&error) {
                tracing::Span::current()
                    .record("operation_id", tracing::field::display(operation_id));
            }
            if !retryable(&error) {
                return Submitted::Unavailable(error, request.items);
            }
            let fields = error_fields(&error);
            if attempt >= TRANSPORT_ATTEMPTS {
                telemetry::transport_exhausted(attempt, &fields);
                return Submitted::Unavailable(error, request.items);
            }
            let wait = jittered(backoff);
            telemetry::transport_retry(&fields, millis(wait));
            match pause(wait, self.deadline, self.cancel).await {
                Ok(()) => {}
                Err(Stop::Deadline(_)) => return Submitted::Unavailable(error, request.items),
                Err(Stop::Cancelled) => return Submitted::Cancelled,
            }
            backoff = backoff.saturating_mul(2);
        }
    }
}

/// One submission's candidates, decided; a refused remainder or split halves go back on
/// `queue` as children of `batch_id`. `Err` is the caller's cancellation.
fn decide(
    submitted: Submitted,
    batch_id: u32,
    queue: &mut Vec<(Option<u32>, Vec<RegisterItem>)>,
) -> Result<Vec<(GtsId, ReconcileOutcome)>, Stop> {
    let pending = |batch: Vec<RegisterItem>, cause: ReconcilePendingCause| {
        batch
            .into_iter()
            .map(|c| (c.gts_id, ReconcileOutcome::Pending(cause.clone())))
            .collect()
    };
    Ok(match submitted {
        Submitted::Decided(outcomes) => outcomes,
        Submitted::Cancelled => return Err(Stop::Cancelled),
        Submitted::Unavailable(error, batch) => pending(batch, ReconcilePendingCause::of(error)),
        Submitted::Refused(error, batch) => refused(error, batch, batch_id, queue),
    })
}

/// A synchronous `InvalidArgument`'s candidates: the named one rejected and the rest
/// resubmitted, a size refusal split, or the whole batch left pending.
fn refused(
    error: CanonicalError,
    mut batch: Vec<RegisterItem>,
    batch_id: u32,
    queue: &mut Vec<(Option<u32>, Vec<RegisterItem>)>,
) -> Vec<(GtsId, ReconcileOutcome)> {
    let fields = error_fields(&error);
    match (refusal(&error, &batch), batch.len()) {
        // Only the named candidate is at fault: reject it, resubmit the rest in order, as a
        // new payload under a new key.
        (Refusal::Candidate(index), _) => {
            let refused = batch.remove(index);
            telemetry::candidate_refused(&refused.gts_id, batch.len(), &fields);
            if !batch.is_empty() {
                queue.push((Some(batch_id), batch));
            }
            vec![(refused.gts_id, ReconcileOutcome::Rejected(error))]
        }
        (Refusal::Size, 2..) => {
            telemetry::batch_split(&fields);
            let right = batch.split_off(batch.len() >> 1);
            queue.push((Some(batch_id), right));
            queue.push((Some(batch_id), batch));
            Vec::new()
        }
        // Splitting cannot cure it, and the documents are not at fault.
        (Refusal::Size | Refusal::Request, _) => {
            telemetry::batch_refused(&fields);
            let cause = ReconcilePendingCause::Refused(error);
            batch
                .into_iter()
                .map(|c| (c.gts_id, ReconcileOutcome::Pending(cause.clone())))
                .collect()
        }
    }
}

fn incomplete_read(id: &GtsId, why: &str) -> CanonicalError {
    tracing::warn!(
        event = "incomplete_read",
        gts_id = %id,
        why,
        "registry read answered incompletely; nothing is submitted"
    );
    CanonicalError::internal(format!(
        "the registry answered a reconciliation read of '{id}' incompletely: {why}"
    ))
    .create()
}

/// What a synchronous `InvalidArgument` refused, from its structured fields only.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Refusal {
    /// `resource_name` names the batch's candidate at this index, and every violation is on
    /// one of a candidate's own fields: only that candidate is at fault.
    Candidate(usize),
    /// No resource is named and every violation is on `items` (the batch's size): splitting
    /// can cure it.
    Size,
    /// Anything else — the publisher, the key, a foreign resource, a `Format`/`Constraint`
    /// shape, or violations mixing those with a candidate's fields: splitting cannot cure
    /// it, and no document is at fault.
    Request,
}

/// The request fields that belong to one candidate.
const CANDIDATE_FIELDS: [&str; 4] = [
    crate::field::GTS_ID_FIELD,
    crate::field::ENTITY_FIELD,
    crate::field::EXPECTED_RESOURCE_VERSION_FIELD,
    crate::field::FORCE_FIELD,
];

fn refusal(error: &CanonicalError, batch: &[RegisterItem]) -> Refusal {
    let CanonicalError::InvalidArgument {
        ctx: InvalidArgument::FieldViolations { field_violations },
        resource_name,
        ..
    } = error
    else {
        return Refusal::Request;
    };
    let only = |fields: &[&str]| {
        !field_violations.is_empty()
            && field_violations
                .iter()
                .all(|v| fields.contains(&v.field.as_str()))
    };
    match resource_name {
        Some(name) if only(&CANDIDATE_FIELDS) => batch
            .iter()
            .position(|c| c.gts_id.id() == name)
            .map_or(Refusal::Request, Refusal::Candidate),
        None if only(&[crate::field::ITEMS_FIELD]) => Refusal::Size,
        _ => Refusal::Request,
    }
}

enum Submitted {
    Decided(Vec<(GtsId, ReconcileOutcome)>),
    /// A synchronous `InvalidArgument` refusal of the whole batch, which is handed back.
    Refused(CanonicalError, Vec<RegisterItem>),
    /// Undecided call: unavailable, refused or expired; returns the batch.
    Unavailable(CanonicalError, Vec<RegisterItem>),
    /// The caller's token stopped the submission.
    Cancelled,
}

/// Require exactly one outcome per submitted candidate; missing/duplicates stay pending, extras are
/// ignored.
fn cover(
    submitted: &[RegisterItem],
    items: Vec<crate::models::RegistrationItemResult>,
) -> Vec<(GtsId, ReconcileOutcome)> {
    let mut reported: HashMap<String, Vec<ReconcileOutcome>> = HashMap::new();
    for item in items {
        reported
            .entry(item.gts_id.id().to_owned())
            .or_default()
            .push(classify(item.outcome));
    }
    submitted
        .iter()
        .map(|candidate| {
            let outcome = match reported.remove(candidate.gts_id.id()) {
                Some(mut outcomes) if outcomes.len() == 1 => outcomes.remove(0),
                found => {
                    tracing::warn!(
                        event = "item_unreported",
                        gts_id = %candidate.gts_id,
                        reported = found.map_or(0, |o| o.len()),
                        "a completed operation did not report this candidate exactly once"
                    );
                    ReconcileOutcome::Pending(ReconcilePendingCause::Unavailable(
                        CanonicalError::internal(
                            "the registry's operation did not report a submitted candidate exactly once",
                        )
                        .create(),
                    ))
                }
            };
            (candidate.gts_id.clone(), outcome)
        })
        .collect()
}

/// Transport/registry availability failures retain [`ReconcilePendingCause::Unavailable`].
fn transport_failure(error: &CanonicalError) -> bool {
    retryable(error)
        || matches!(
            error,
            CanonicalError::DeadlineExceeded { .. } | CanonicalError::ResourceExhausted { .. }
        )
}

/// Transport failures worth repeating under the same key and request.
fn retryable(error: &CanonicalError) -> bool {
    matches!(
        error,
        CanonicalError::Aborted { .. }
            | CanonicalError::ServiceUnavailable { .. }
            | CanonicalError::Internal { .. }
            | CanonicalError::Unknown { .. }
    )
}

/// Classify candidate outcomes by admission-failure reason.
fn classify(outcome: RegistrationOutcome) -> ReconcileOutcome {
    match outcome {
        RegistrationOutcome::Succeeded { .. } | RegistrationOutcome::Unchanged { .. } => {
            ReconcileOutcome::Admitted
        }
        RegistrationOutcome::Pending | RegistrationOutcome::Running => {
            ReconcileOutcome::Pending(ReconcilePendingCause::Unavailable(
                CanonicalError::internal("a completed operation left a candidate undecided")
                    .create(),
            ))
        }
        // Reconciliation never submits a dry run: a preview is not a write.
        RegistrationOutcome::WouldSucceed => {
            ReconcileOutcome::Pending(ReconcilePendingCause::Unavailable(
                CanonicalError::internal(
                    "the registry answered a committing registration with a dry-run preview",
                )
                .create(),
            ))
        }
        RegistrationOutcome::Failed { error } => {
            let Some(failure) = AdmissionFailure::from_canonical(&error) else {
                return ReconcileOutcome::Rejected(error);
            };
            match failure.reason {
                Reason::DependencyNotFound
                | Reason::BlockedByDependency
                | Reason::BlockedByPredecessor
                | Reason::MissingPredecessor => {
                    ReconcileOutcome::Pending(ReconcilePendingCause::Dependency(error))
                }
                // Contention with concurrent writers: re-read and resubmit next pass.
                Reason::AlreadyExists | Reason::PreconditionFailed | Reason::RevalidationExhausted => {
                    ReconcileOutcome::Pending(ReconcilePendingCause::Conflict(error))
                }
                Reason::SystemFailure => {
                    ReconcileOutcome::Pending(ReconcilePendingCause::Unavailable(error))
                }
                Reason::ActivationWriteSetExceeded
                | Reason::BaselineUnresolvable
                | Reason::CompatibilityUndecidable
                | Reason::DependencyDeleted
                | Reason::DependentInvalid
                | Reason::DialectChanged
                | Reason::EntityDeleted
                | Reason::FamilyKindConflict
                | Reason::HasRegisteredDependents
                | Reason::FamilyShapeConflict
                | Reason::IncompatibleWithBaseline
                | Reason::InstanceOfMajorZero
                | Reason::InvalidDocument
                | Reason::InvalidIdentifier
                | Reason::InvalidSchema
                | Reason::InvalidValue
                | Reason::NotActive
                | Reason::PublisherMismatch
                | Reason::ResolutionClosureExceeded
                | Reason::ResolvedDocumentTooLarge
                | Reason::StableDerivesFromMajorZero
                | Reason::StableRefsMajorZero
                | Reason::Superseded
                | Reason::UnparsablePayload
                | Reason::UnreadableVersion
                | Reason::UnrecognizedPayload
                // A reason this build does not know is not retried on a guess.
                | Reason::Unknown(_) => ReconcileOutcome::Rejected(error),
            }
        }
    }
}

#[cfg(test)]
#[path = "mod_tests.rs"]
mod mod_tests;

#[cfg(test)]
mod telemetry_tests;

#[cfg(test)]
mod telemetry_capture;
