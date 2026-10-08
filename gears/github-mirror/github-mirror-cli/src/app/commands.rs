use std::collections::BTreeMap;
use std::num::NonZeroUsize;
use std::path::Path;

use anyhow::{Context, Result, anyhow, bail};
use chrono::{DateTime, Days, NaiveDate, TimeZone, Utc};
use github_mirror::api::rest::dto::{
    BranchDto, CommentDto, CommitDto, ContributorDto, IssueDto, IssueReactionDto,
    IssueTimelineEventDto, LabelDto, MilestoneDto, PullRequestDto, ReleaseDto, RepoDto,
    RepoSyncStatusDto, ReviewCommentDto, ReviewDto, ReviewThreadDto, SyncSessionDto,
    SyncSummaryDto, WorkflowRunDto,
};
use github_mirror::domain::ports::github::ForceMode;
use github_mirror::domain::repo::{
    ListingFilter, PageWindow, RepoSyncStatusRecord, SyncSessionRecord,
};
use github_mirror::domain::scope::{CollectionMode, SyncScope};
use github_mirror::domain::service::{Service, SyncRequest};
use serde::Serialize;
use serde_json::{Value, json};
use toolkit_odata::{CursorV1, ODataQuery};
use toolkit_security::SecurityContext;

const PAGE_LIMIT: u64 = 100;

#[derive(Clone, Copy, clap::ValueEnum)]
pub enum Entity {
    Issues,
    Prs,
    Commits,
    Contributors,
    Repos,
    Comments,
    Conversations,
    ReviewThreads,
    Reviews,
    ReviewComments,
    Reactions,
    Timeline,
    Branches,
    Labels,
    Milestones,
    Releases,
    #[value(name = "workflow_runs")]
    WorkflowRuns,
}

pub struct SyncFlags<'a> {
    pub force: ForceMode,
    pub max_concurrent: Option<NonZeroUsize>,
    pub since: Option<&'a str>,
    pub include: Option<&'a str>,
    pub exclude: Option<&'a str>,
    pub actions_scope: Option<&'a str>,
    pub reactions_scope: Option<&'a str>,
    pub timeline_scope: Option<&'a str>,
}

pub fn sync_request(service: &Service, flags: &SyncFlags<'_>) -> Result<SyncRequest> {
    let narrows = flags.include.is_some()
        || flags.exclude.is_some()
        || flags.actions_scope.is_some()
        || flags.reactions_scope.is_some()
        || flags.timeline_scope.is_some();
    let scope = if narrows {
        let mut scope = service.default_scope();
        if let Some(include) = flags.include {
            scope.objects = SyncScope::parse_list(include)?;
        }
        if let Some(exclude) = flags.exclude {
            scope.objects = scope.objects.without(SyncScope::parse_list(exclude)?);
        }
        if let Some(mode) = flags.actions_scope {
            scope.collection.actions = CollectionMode::parse(mode)?;
        }
        if let Some(mode) = flags.reactions_scope {
            scope.collection.reactions = CollectionMode::parse(mode)?;
        }
        if let Some(mode) = flags.timeline_scope {
            scope.collection.timeline = CollectionMode::parse(mode)?;
        }
        Some(scope)
    } else {
        None
    };
    Ok(SyncRequest {
        scope,
        force: flags.force,
        since: flags.since.map(parse_since).transpose()?,
        max_concurrent_tasks: flags.max_concurrent,
    })
}

fn parse_since(raw: &str) -> Result<DateTime<Utc>> {
    let value = raw.trim();
    if let Ok(date) = NaiveDate::parse_from_str(value, "%Y-%m-%d") {
        return date
            .and_hms_opt(0, 0, 0)
            .map(|at| Utc.from_utc_datetime(&at))
            .ok_or_else(|| anyhow!("`{value}` is not a date"));
    }
    let (number, unit) = value.split_at(value.len().saturating_sub(1));
    let amount: u64 = number
        .parse()
        .ok()
        .filter(|amount| *amount > 0)
        .ok_or_else(|| anyhow!("`{value}` is not YYYY-MM-DD, Nd, Nw or Nm"))?;
    let days = match unit {
        "d" => amount,
        "w" => amount.saturating_mul(7),
        "m" => amount.saturating_mul(30),
        _ => bail!("`{value}` is not YYYY-MM-DD, Nd, Nw or Nm"),
    };
    Utc::now()
        .checked_sub_days(Days::new(days))
        .ok_or_else(|| anyhow!("`{value}` reaches back too far"))
}

pub async fn sync(
    service: &Service,
    ctx: &SecurityContext,
    repo: &str,
    request: SyncRequest,
) -> Result<Value> {
    let (owner, name) = split_repo(repo)?;
    let summary = service.sync_now(ctx, owner, name, None, request).await?;
    Ok(serde_json::to_value(SyncSummaryDto::from(summary))?)
}

pub async fn query(
    service: &Service,
    ctx: &SecurityContext,
    entity: Entity,
    repo: &str,
    number: Option<i64>,
    limit: u64,
) -> Result<Value> {
    let (owner, name) = split_repo(repo)?;
    let window = PageWindow::bounded(limit, 0)?;
    let filter = ListingFilter::default();
    let rows = match entity {
        Entity::Issues => rows(
            service
                .list_issues(ctx, owner, name, window, filter)
                .await?
                .0
                .items,
            IssueDto::from,
        )?,
        Entity::Prs => rows(
            service
                .list_pull_requests(ctx, owner, name, window, filter)
                .await?
                .0
                .items,
            PullRequestDto::from,
        )?,
        Entity::Commits => rows(
            service
                .list_commits(ctx, owner, name, window, None)
                .await?
                .0
                .items,
            CommitDto::from,
        )?,
        Entity::Contributors => rows(
            service
                .list_contributors(ctx, owner, name, window)
                .await?
                .items,
            ContributorDto::from,
        )?,
        Entity::Repos => vec![serde_json::to_value(RepoDto::from(
            service.get_repo(ctx, owner, name).await?,
        ))?],
        Entity::Branches => rows(
            service.list_branches(ctx, owner, name, window).await?.items,
            BranchDto::from,
        )?,
        Entity::Labels => rows(
            service.list_labels(ctx, owner, name, window).await?.items,
            LabelDto::from,
        )?,
        Entity::Milestones => rows(
            service
                .list_milestones(ctx, owner, name, window)
                .await?
                .items,
            MilestoneDto::from,
        )?,
        Entity::Releases => rows(
            service.list_releases(ctx, owner, name, window).await?.items,
            ReleaseDto::from,
        )?,
        Entity::WorkflowRuns => rows(
            service
                .list_workflow_runs(ctx, owner, name, window)
                .await?
                .0
                .items,
            WorkflowRunDto::from,
        )?,
        Entity::Conversations => conversations(service, ctx, (owner, name), number, limit).await?,
        Entity::Comments
        | Entity::ReviewThreads
        | Entity::Reviews
        | Entity::ReviewComments
        | Entity::Reactions
        | Entity::Timeline => {
            let number = number.ok_or_else(|| {
                anyhow!("this entity belongs to one issue or pull request: pass --number <N>")
            })?;
            query_one(service, ctx, entity, (owner, name), number, window).await?
        }
    };
    Ok(Value::Array(rows))
}

async fn query_one(
    service: &Service,
    ctx: &SecurityContext,
    entity: Entity,
    (owner, name): (&str, &str),
    number: i64,
    window: PageWindow,
) -> Result<Vec<Value>> {
    match entity {
        Entity::Comments => rows(
            service
                .list_comments(ctx, owner, name, number, window)
                .await?
                .items,
            CommentDto::from,
        ),
        Entity::ReviewThreads => rows(
            service
                .list_review_threads(
                    ctx,
                    owner,
                    name,
                    number,
                    &ODataQuery::new().with_limit(window.limit()),
                )
                .await?
                .items,
            ReviewThreadDto::from,
        ),
        Entity::Reviews => rows(
            service
                .list_reviews(ctx, owner, name, number, window)
                .await?
                .items,
            ReviewDto::from,
        ),
        Entity::ReviewComments => rows(
            service
                .list_review_comments(ctx, owner, name, number, window)
                .await?
                .items,
            ReviewCommentDto::from,
        ),
        Entity::Reactions => rows(
            service
                .list_issue_reactions(ctx, owner, name, number, window)
                .await?
                .items,
            IssueReactionDto::from,
        ),
        Entity::Timeline => rows(
            service
                .list_issue_timeline(ctx, owner, name, number, window)
                .await?
                .items,
            IssueTimelineEventDto::from,
        ),
        Entity::Issues
        | Entity::Prs
        | Entity::Commits
        | Entity::Contributors
        | Entity::Repos
        | Entity::Branches
        | Entity::Labels
        | Entity::Milestones
        | Entity::Releases
        | Entity::WorkflowRuns
        | Entity::Conversations => Ok(Vec::new()),
    }
}

async fn conversations(
    service: &Service,
    ctx: &SecurityContext,
    (owner, name): (&str, &str),
    number: Option<i64>,
    limit: u64,
) -> Result<Vec<Value>> {
    let mut parents: BTreeMap<(String, i64), Vec<Value>> = BTreeMap::new();
    let found = service.list_conversations(ctx, owner, name, number).await?;
    for conversation in found
        .into_iter()
        .take(usize::try_from(limit).unwrap_or(usize::MAX))
    {
        let members = service.conversation_comments(ctx, &conversation).await?;
        let comments = if members.review_comments.is_empty() {
            rows(members.comments, CommentDto::from)?
        } else {
            rows(members.review_comments, ReviewCommentDto::from)?
        };
        parents
            .entry((conversation.parent_kind, conversation.parent_number))
            .or_default()
            .push(json!({
                "conv_type": conversation.conv_type,
                "root_comment_id": conversation.root_comment_id,
                "comment_count": conversation.comment_count,
                "is_resolved": conversation.is_resolved,
                "created_at": conversation.created_at,
                "comments": comments,
            }));
    }
    Ok(parents
        .into_iter()
        .map(|((parent_kind, parent_number), conversations)| {
            json!({
                "parent_kind": parent_kind,
                "parent_number": parent_number,
                "conversations": conversations,
            })
        })
        .collect())
}

pub async fn check_rate_limit(service: &Service, ctx: &SecurityContext) -> Result<Value> {
    let quotas = service.rate_limit(ctx).await?;
    let now = chrono::Utc::now();
    let rows: Vec<Value> = quotas
        .iter()
        .map(|quota| {
            json!({
                "resource": quota.resource,
                "limit": quota.limit,
                "used": quota.used,
                "remaining": quota.remaining,
                "reset_at": quota.reset_at.map(|at| at.to_rfc3339()),
                "reset_in_seconds": quota.reset_at.map(|at| (at - now).num_seconds().max(0)),
            })
        })
        .collect();
    Ok(Value::Array(rows))
}

pub async fn clear_cache(service: &Service, ctx: &SecurityContext, repo: &str) -> Result<Value> {
    let (owner, name) = split_repo(repo)?;
    let removed = service.delete_repository(ctx, owner, name).await?;
    Ok(json!({ "repository": format!("{owner}/{name}"), "rows_removed": removed }))
}

pub async fn status(
    service: &Service,
    ctx: &SecurityContext,
    repo: &str,
    database_file: Option<&Path>,
) -> Result<Value> {
    let (owner, name) = split_repo(repo)?;
    let full_name = format!("{owner}/{name}");
    let run = run_status(service, ctx, &full_name)
        .await?
        .map(RepoSyncStatusDto::from);
    let session = latest_session(service, ctx, &full_name)
        .await?
        .map(SyncSessionDto::from);
    let cache_bytes = service.cache_size(ctx, owner, name).await?;
    let database_bytes = database_file.map(database_bytes).transpose()?;
    Ok(json!({
        "repository": full_name,
        "run": run,
        "last_session": session,
        "storage": {
            "cache_bytes": cache_bytes,
            "database_bytes": database_bytes,
        },
    }))
}

fn database_bytes(path: &Path) -> Result<u64> {
    let mut wal = path.as_os_str().to_owned();
    wal.push("-wal");
    Ok(file_size(path)? + file_size(Path::new(&wal))?)
}

fn file_size(path: &Path) -> Result<u64> {
    match std::fs::metadata(path) {
        Ok(meta) => Ok(meta.len()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(0),
        Err(e) => Err(e).with_context(|| format!("cannot read {}", path.display())),
    }
}

async fn run_status(
    service: &Service,
    ctx: &SecurityContext,
    full_name: &str,
) -> Result<Option<RepoSyncStatusRecord>> {
    let mut query = ODataQuery::new().with_limit(PAGE_LIMIT);
    loop {
        let page = service.list_repo_sync_status(ctx, &query, None).await?;
        if let Some(found) = page
            .items
            .into_iter()
            .find(|row| row.repo_full_name.eq_ignore_ascii_case(full_name))
        {
            return Ok(Some(found));
        }
        let Some(next) = page.page_info.next_cursor else {
            return Ok(None);
        };
        query = next_page(&next)?;
    }
}

async fn latest_session(
    service: &Service,
    ctx: &SecurityContext,
    full_name: &str,
) -> Result<Option<SyncSessionRecord>> {
    let mut query = ODataQuery::new().with_limit(PAGE_LIMIT);
    loop {
        let page = service.list_sessions(ctx, &query).await?;
        if let Some(found) = page
            .items
            .into_iter()
            .find(|row| row.repo_full_name.eq_ignore_ascii_case(full_name))
        {
            return Ok(Some(found));
        }
        let Some(next) = page.page_info.next_cursor else {
            return Ok(None);
        };
        query = next_page(&next)?;
    }
}

fn next_page(cursor: &str) -> Result<ODataQuery> {
    let cursor = CursorV1::decode(cursor)
        .map_err(|e| anyhow!("the next-page cursor did not decode: {e}"))?;
    Ok(ODataQuery::new().with_limit(PAGE_LIMIT).with_cursor(cursor))
}

fn rows<T, D: Serialize>(items: Vec<T>, to_dto: impl Fn(T) -> D) -> Result<Vec<Value>> {
    items
        .into_iter()
        .map(|item| serde_json::to_value(to_dto(item)).map_err(Into::into))
        .collect()
}

fn split_repo(repo: &str) -> Result<(&str, &str)> {
    repo.split_once('/')
        .filter(|(owner, name)| !owner.is_empty() && !name.is_empty() && !name.contains('/'))
        .ok_or_else(|| anyhow!("`{repo}` is not ORG/REPO"))
}
