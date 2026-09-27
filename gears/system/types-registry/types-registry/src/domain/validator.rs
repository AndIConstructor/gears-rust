//! Freshness validators (SPEC §8.5): computed per read, never stored.

use aws_lc_rs::digest::{Context, SHA256};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use toolkit_macros::domain_model;

use crate::domain::selection::FieldSelection;

/// ponytail: ceiling C7 — no visibility or availability inputs yet; P1 adds them
/// under a new version, which a v1 token then never matches.
const VERSION: u8 = 1;

/// A 128-bit digest of what a representation depends on. Equality is the only
/// operation.
#[domain_model]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Validator([u8; 16]);

/// The decoded wire object: field order and spelling are not part of the identity.
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Wire {
    v: u8,
    d: String,
}

impl Validator {
    /// Instances pass no fingerprint: they have no derived form.
    #[must_use]
    pub fn compute(
        resource_version: i64,
        resolution_fingerprint: Option<&[u8]>,
        selection: FieldSelection,
    ) -> Self {
        let mut ctx = Context::new(&SHA256);
        ctx.update(&[VERSION]);
        ctx.update(&resource_version.to_be_bytes());
        match resolution_fingerprint {
            None => ctx.update(&[0]),
            Some(fingerprint) => {
                ctx.update(&[1]);
                update_prefixed(&mut ctx, fingerprint);
            }
        }
        update_prefixed(&mut ctx, selection.canonical().as_bytes());
        let mut digest = [0; 16];
        digest.copy_from_slice(&ctx.finish().as_ref()[..16]);
        Self(digest)
    }

    /// base64url of `{"v":1,"d":"<base64url digest>"}`; neither value needs escaping.
    #[must_use]
    pub fn encode(self) -> String {
        let digest = URL_SAFE_NO_PAD.encode(self.0);
        URL_SAFE_NO_PAD.encode(format!(r#"{{"v":{VERSION},"d":"{digest}"}}"#))
    }

    /// `None` for anything but a well-formed current-version token, which the
    /// caller then answers with a full result (DESIGN §3.3).
    #[must_use]
    pub fn decode(token: &str) -> Option<Self> {
        let json = URL_SAFE_NO_PAD.decode(token).ok()?;
        let wire: Wire = serde_json::from_slice(&json).ok()?;
        if wire.v != VERSION {
            return None;
        }
        let digest = URL_SAFE_NO_PAD.decode(wire.d).ok()?;
        Some(Self(digest.try_into().ok()?))
    }
}

/// Length-prefixed, so no two field splits digest alike.
fn update_prefixed(ctx: &mut Context, bytes: &[u8]) {
    ctx.update(&(bytes.len() as u64).to_be_bytes());
    ctx.update(bytes);
}

#[cfg(test)]
#[path = "validator_tests.rs"]
mod validator_tests;
