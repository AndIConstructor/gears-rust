use std::sync::Arc;

use async_trait::async_trait;
use authz_resolver_sdk::PolicyEnforcer;
use authz_resolver_sdk::pep::{AccessRequest, ResourceType};
use toolkit_db::secure::DBRunner;
use toolkit_macros::domain_model;
use toolkit_security::{AccessScope, SecurityContext, pep_properties};
use uuid::Uuid;

use super::DbProvider;
use super::error::DomainError;

/// Authorization resource type for subject settings, scoped by tenant and
/// subject id.
pub(crate) const SUBJECT_SETTINGS_RESOURCE: ResourceType = ResourceType::from_static(
    "construct.subject_settings",
    &[pep_properties::OWNER_TENANT_ID, pep_properties::RESOURCE_ID],
);

pub(crate) mod actions {
    pub const GET: &str = "get";
    pub const UPDATE: &str = "update";
}

/// The two states of one subject that intake, admission and the reader read.
#[domain_model]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SubjectSettings {
    pub personalization_enabled: bool,
    pub erasure_in_progress: bool,
}

impl SubjectSettings {
    /// The settings of a subject Construct holds no row for: the tenant's
    /// personalization default, and no erasure under way.
    #[must_use]
    pub fn new_subject(personalization_default: bool) -> Self {
        Self {
            personalization_enabled: personalization_default,
            erasure_in_progress: false,
        }
    }
}

#[async_trait]
pub trait SubjectSettingsRepository: Send + Sync {
    /// The stored settings of one subject, if they are inside the scope.
    async fn find<C: DBRunner>(
        &self,
        conn: &C,
        scope: &AccessScope,
        subject_id: Uuid,
    ) -> Result<Option<SubjectSettings>, DomainError>;

    /// Store the subject's personalization setting in `tenant_id`, creating the
    /// row when there is none. A new row starts with no erasure under way; an
    /// existing row keeps its erasure flag. The scope must cover the row.
    async fn set_personalization<C: DBRunner>(
        &self,
        conn: &C,
        scope: &AccessScope,
        tenant_id: Uuid,
        subject_id: Uuid,
        personalization_enabled: bool,
    ) -> Result<(), DomainError>;
}

/// Owner only: a subject reads and changes only its own settings. Another
/// subject, in this tenant or another, is refused.
fn ensure_owner(ctx: &SecurityContext, subject_id: Uuid) -> Result<(), DomainError> {
    // @cpt-begin:cpt-cf-construct-algo-subject-settings-owner-only:p1:inst-owner-compare
    if ctx.subject_id() == subject_id {
        return Ok(());
    }
    // @cpt-end:cpt-cf-construct-algo-subject-settings-owner-only:p1:inst-owner-compare
    // @cpt-begin:cpt-cf-construct-algo-subject-settings-owner-only:p1:inst-owner-refuse
    Err(DomainError::forbidden(
        "a subject may only read and change its own settings",
    ))
    // @cpt-end:cpt-cf-construct-algo-subject-settings-owner-only:p1:inst-owner-refuse
}

/// The tenant settings a new subject starts from. The tenant settings live in
/// the settings service; Construct keeps none of its own.
#[async_trait]
pub trait TenantDefaults: Send + Sync {
    /// Whether personalization is on for a new subject of the tenant.
    async fn personalization_default(
        &self,
        ctx: &SecurityContext,
        tenant_id: Uuid,
    ) -> Result<bool, DomainError>;
}

#[domain_model]
pub struct SubjectSettingsService<R: SubjectSettingsRepository> {
    db: Arc<DbProvider>,
    repo: Arc<R>,
    defaults: Arc<dyn TenantDefaults>,
    policy_enforcer: PolicyEnforcer,
}

/// @cpt-dod:cpt-cf-construct-dod-subject-settings-read:p1
impl<R: SubjectSettingsRepository> SubjectSettingsService<R> {
    pub fn new(
        db: Arc<DbProvider>,
        repo: Arc<R>,
        defaults: Arc<dyn TenantDefaults>,
        policy_enforcer: PolicyEnforcer,
    ) -> Self {
        Self {
            db,
            repo,
            defaults,
            policy_enforcer,
        }
    }

    /// The settings of one subject in the caller's tenant. A subject without a
    /// row gets the tenant's default; reading does not store it.
    #[tracing::instrument(skip_all, fields(tenant_id = %ctx.subject_tenant_id(), subject_id = %subject_id))]
    pub async fn settings(
        &self,
        ctx: &SecurityContext,
        subject_id: Uuid,
    ) -> Result<SubjectSettings, DomainError> {
        // @cpt-begin:cpt-cf-construct-algo-subject-settings-read:p1:inst-read-scope
        let tenant_id = ctx.subject_tenant_id();
        let scope = self
            .policy_enforcer
            .access_scope_with(
                ctx,
                &SUBJECT_SETTINGS_RESOURCE,
                actions::GET,
                Some(subject_id),
                &AccessRequest::new().resource_property(pep_properties::OWNER_TENANT_ID, tenant_id),
            )
            .await?;
        // @cpt-end:cpt-cf-construct-algo-subject-settings-read:p1:inst-read-scope

        // @cpt-begin:cpt-cf-construct-algo-subject-settings-read:p1:inst-read-select
        let conn = self.db.conn().map_err(DomainError::from)?;
        let stored = self.repo.find(&conn, &scope, subject_id).await?;
        // @cpt-end:cpt-cf-construct-algo-subject-settings-read:p1:inst-read-select

        // @cpt-begin:cpt-cf-construct-algo-subject-settings-read:p1:inst-read-stored
        if let Some(stored) = stored {
            return Ok(stored);
        }
        // @cpt-end:cpt-cf-construct-algo-subject-settings-read:p1:inst-read-stored

        // @cpt-begin:cpt-cf-construct-algo-subject-settings-read:p1:inst-read-default
        let personalization_default = self
            .defaults
            .personalization_default(ctx, tenant_id)
            .await?;
        Ok(SubjectSettings::new_subject(personalization_default))
        // @cpt-end:cpt-cf-construct-algo-subject-settings-read:p1:inst-read-default
    }

    /// The subject's own settings, for the subject's settings route. Owner
    /// only; otherwise the same read as [`Self::settings`].
    ///
    /// @cpt-dod:cpt-cf-construct-dod-subject-settings-routes:p1
    #[tracing::instrument(skip_all, fields(tenant_id = %ctx.subject_tenant_id(), subject_id = %subject_id))]
    pub async fn owner_settings(
        &self,
        ctx: &SecurityContext,
        subject_id: Uuid,
    ) -> Result<SubjectSettings, DomainError> {
        // @cpt-begin:cpt-cf-construct-flow-subject-settings-view:p1:inst-view-owner
        ensure_owner(ctx, subject_id)?;
        // @cpt-end:cpt-cf-construct-flow-subject-settings-view:p1:inst-view-owner
        // @cpt-begin:cpt-cf-construct-flow-subject-settings-view:p1:inst-view-read
        self.settings(ctx, subject_id).await
        // @cpt-end:cpt-cf-construct-flow-subject-settings-view:p1:inst-view-read
    }

    /// Turn personalization on or off for the subject, which is the caller.
    /// The erasure flag is left as it is, so turning personalization on during
    /// an erasure stores the choice but does not end the erasure. Returns the
    /// settings as stored.
    #[tracing::instrument(skip_all, fields(tenant_id = %ctx.subject_tenant_id(), subject_id = %subject_id))]
    pub async fn set_personalization(
        &self,
        ctx: &SecurityContext,
        subject_id: Uuid,
        personalization_enabled: bool,
    ) -> Result<SubjectSettings, DomainError> {
        // @cpt-begin:cpt-cf-construct-flow-subject-settings-change:p1:inst-change-owner
        ensure_owner(ctx, subject_id)?;
        // @cpt-end:cpt-cf-construct-flow-subject-settings-change:p1:inst-change-owner

        // @cpt-begin:cpt-cf-construct-flow-subject-settings-change:p1:inst-change-scope
        let tenant_id = ctx.subject_tenant_id();
        let scope = self
            .policy_enforcer
            .access_scope_with(
                ctx,
                &SUBJECT_SETTINGS_RESOURCE,
                actions::UPDATE,
                Some(subject_id),
                &AccessRequest::new().resource_property(pep_properties::OWNER_TENANT_ID, tenant_id),
            )
            .await?;
        // @cpt-end:cpt-cf-construct-flow-subject-settings-change:p1:inst-change-scope

        // @cpt-begin:cpt-cf-construct-flow-subject-settings-change:p1:inst-change-store
        let conn = self.db.conn().map_err(DomainError::from)?;
        self.repo
            .set_personalization(
                &conn,
                &scope,
                tenant_id,
                subject_id,
                personalization_enabled,
            )
            .await?;
        // @cpt-end:cpt-cf-construct-flow-subject-settings-change:p1:inst-change-store

        // @cpt-begin:cpt-cf-construct-flow-subject-settings-change:p1:inst-change-return
        self.repo
            .find(&conn, &scope, subject_id)
            .await?
            .ok_or_else(|| DomainError::internal("subject settings row missing after the write"))
        // @cpt-end:cpt-cf-construct-flow-subject-settings-change:p1:inst-change-return
    }
}
