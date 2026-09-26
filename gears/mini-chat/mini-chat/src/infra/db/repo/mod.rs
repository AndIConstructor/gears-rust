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
    use toolkit_odata::Error as E;
    // Exhaustive on purpose: a new variant must be classified here.
    match e {
        E::Db(msg) => DomainError::database(msg),
        E::ParsingUnavailable(msg) => DomainError::database(msg),
        client @ (E::InvalidFilter(_)
        | E::InvalidOrderByField(_)
        | E::OrderMismatch
        | E::FilterMismatch
        | E::InvalidCursor
        | E::InvalidLimit
        | E::OrderWithCursor
        | E::CursorInvalidBase64
        | E::CursorInvalidJson
        | E::CursorInvalidVersion
        | E::CursorInvalidKeys
        | E::CursorInvalidFields
        | E::CursorInvalidDirection) => DomainError::OData(client),
    }
}

#[cfg(test)]
#[cfg_attr(coverage_nightly, coverage(off))]
#[path = "repo_test.rs"]
mod repo_test;
