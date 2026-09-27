//! HTTP entity-tag framing of the domain validator (RFC 9110 §8.8.3). The exact
//! read's `ETag` and a batch result's `etag` carry identical bytes (SPEC §8.5).

use axum::http::{HeaderMap, header};

use crate::domain::validator::{IfNoneMatch, Validator};

/// A strong entity-tag: the validator, quoted.
pub fn entity_tag(etag: Validator) -> String {
    format!("\"{}\"", etag.encode())
}

/// The validator inside an entity-tag. `W/` is dropped, as the weak comparison
/// `If-None-Match` uses requires; anything unquoted cannot name a validator.
fn opaque(tag: &str) -> Option<&str> {
    let tag = tag.trim();
    let tag = tag.strip_prefix("W/").unwrap_or(tag);
    tag.strip_prefix('"')?.strip_suffix('"')
}

/// A batch item's `if_none_match`, one entity-tag.
pub fn item_condition(tag: &str) -> IfNoneMatch {
    IfNoneMatch::Validators(opaque(tag).map(str::to_owned).into_iter().collect())
}

/// The `If-None-Match` header: `*` or a list of entity-tags (RFC 9110 §13.1.2).
pub fn header_condition(headers: &HeaderMap) -> Option<IfNoneMatch> {
    let values: Vec<&str> = headers
        .get_all(header::IF_NONE_MATCH)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .collect();
    if values.is_empty() {
        return None;
    }
    if values.iter().any(|value| value.trim() == "*") {
        return Some(IfNoneMatch::Any);
    }
    let tags = values
        .iter()
        .flat_map(|value| value.split(','))
        .filter_map(opaque)
        .map(str::to_owned)
        .collect();
    Some(IfNoneMatch::Validators(tags))
}
