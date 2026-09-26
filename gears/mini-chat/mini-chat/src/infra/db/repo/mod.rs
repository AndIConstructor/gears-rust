pub mod attachment_repo;
pub mod chat_repo;
pub mod message_attachment_repo;
pub mod message_repo;
pub mod quota_usage_repo;
pub mod reaction_repo;
pub mod thread_summary_repo;
pub mod turn_repo;
pub mod vector_store_repo;

use crate::domain::error::DomainError;

/// Maps an `OData` pagination error: a bad `$filter`, `$orderby`, `limit` or
/// cursor is the client's fault (400); only DB/config failures are internal.
pub(crate) fn odata_err(e: toolkit_odata::Error) -> DomainError {
    match e {
        toolkit_odata::Error::Db(msg) => DomainError::database(msg),
        toolkit_odata::Error::ParsingUnavailable(msg) => DomainError::database(msg),
        client => DomainError::validation(client.to_string()),
    }
}

#[cfg(test)]
#[cfg_attr(coverage_nightly, coverage(off))]
#[path = "repo_test.rs"]
mod repo_test;
