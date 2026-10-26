use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use toolkit_utils::SecretString;
use tracing::warn;

use super::controller::{RateLimitController, Readiness};
use super::registry::{ControllerRegistry, TokenFingerprint};

pub struct PooledToken {
    pub token: Option<SecretString>,
    pub controller: Arc<RateLimitController>,
    fingerprint: Option<TokenFingerprint>,
    dropped: AtomicBool,
}

impl PooledToken {
    /// Take this token out of the pool after GitHub answered `401` to it, so
    /// the request can be retried with another token. `false` for the entry
    /// without a token, whose `401` stays an error.
    pub fn drop_from_pool(&self) -> bool {
        let Some(fingerprint) = &self.fingerprint else {
            return false;
        };
        if !self.dropped.swap(true, Ordering::AcqRel) {
            warn!(
                token = fingerprint.short(),
                "GitHub answered 401 Unauthorized to a pool token; it stays out of the pool until a restart"
            );
        }
        true
    }
}

pub struct TokenPool {
    tokens: Vec<PooledToken>,
}

impl TokenPool {
    #[must_use]
    pub fn new(tokens: Vec<SecretString>, registry: &ControllerRegistry) -> Self {
        if tokens.is_empty() {
            return Self {
                tokens: vec![PooledToken {
                    token: None,
                    controller: Arc::new(RateLimitController::new()),
                    fingerprint: None,
                    dropped: AtomicBool::new(false),
                }],
            };
        }
        Self {
            tokens: tokens
                .into_iter()
                .map(|token| {
                    let fingerprint = TokenFingerprint::of(token.expose());
                    let controller = registry.get_or_insert(fingerprint.clone());
                    PooledToken {
                        token: Some(token),
                        controller,
                        fingerprint: Some(fingerprint),
                        dropped: AtomicBool::new(false),
                    }
                })
                .collect(),
        }
    }

    #[must_use]
    pub fn first(&self) -> &PooledToken {
        &self.tokens[0]
    }

    pub fn iter(&self) -> impl Iterator<Item = &PooledToken> {
        self.tokens.iter()
    }

    pub async fn pick(&self, graphql: bool) -> Option<&PooledToken> {
        let mut picked: Option<(&PooledToken, (u8, i64))> = None;
        for pooled in self
            .tokens
            .iter()
            .filter(|pooled| !pooled.dropped.load(Ordering::Acquire))
        {
            let rank = rank(pooled.controller.readiness(graphql).await);
            if picked.is_none_or(|(_, best)| rank < best) {
                picked = Some((pooled, rank));
            }
        }
        picked.map(|(pooled, _)| pooled)
    }
}

fn rank(readiness: Readiness) -> (u8, i64) {
    match readiness {
        Readiness::Now(spare) => (0, -i64::from(spare)),
        Readiness::Later(Some(at)) => (1, at.timestamp_millis()),
        Readiness::Later(None) => (2, 0),
    }
}
