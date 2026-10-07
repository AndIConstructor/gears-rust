use anyhow::{Result, anyhow};
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
use github_mirror::domain::service::Service;
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

pub async fn sync(
    service: &Service,
    ctx: &SecurityContext,
    repo: &str,
    force: ForceMode,
) -> Result<Value> {
    let (owner, name) = split_repo(repo)?;
    let summary = service.sync_now(ctx, owner, name, None, force).await?;
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
        | Entity::WorkflowRuns => Ok(Vec::new()),
    }
}

pub async fn clear_cache(service: &Service, ctx: &SecurityContext, repo: &str) -> Result<Value> {
    let (owner, name) = split_repo(repo)?;
    let removed = service.delete_repository(ctx, owner, name).await?;
    Ok(json!({ "repository": format!("{owner}/{name}"), "rows_removed": removed }))
}

pub async fn status(service: &Service, ctx: &SecurityContext, repo: &str) -> Result<Value> {
    let (owner, name) = split_repo(repo)?;
    let full_name = format!("{owner}/{name}");
    let run = run_status(service, ctx, &full_name)
        .await?
        .map(RepoSyncStatusDto::from);
    let session = latest_session(service, ctx, &full_name)
        .await?
        .map(SyncSessionDto::from);
    Ok(json!({
        "repository": full_name,
        "run": run,
        "last_session": session,
    }))
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
