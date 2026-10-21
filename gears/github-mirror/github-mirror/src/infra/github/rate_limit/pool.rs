use std::sync::Arc;

use toolkit_utils::SecretString;

use super::controller::{RateLimitController, Readiness};
use super::registry::{ControllerRegistry, TokenFingerprint};

pub struct PooledToken {
    pub token: Option<SecretString>,
    pub controller: Arc<RateLimitController>,
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
                }],
            };
        }
        Self {
            tokens: tokens
                .into_iter()
                .map(|token| {
                    let controller = registry.get_or_insert(TokenFingerprint::of(token.expose()));
                    PooledToken {
                        token: Some(token),
                        controller,
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

    pub async fn pick(&self, graphql: bool) -> &PooledToken {
        let mut picked = &self.tokens[0];
        let mut picked_rank = rank(picked.controller.readiness(graphql).await);
        for pooled in self.tokens.iter().skip(1) {
            let rank = rank(pooled.controller.readiness(graphql).await);
            if rank < picked_rank {
                picked = pooled;
                picked_rank = rank;
            }
        }
        picked
    }
}

fn rank(readiness: Readiness) -> (u8, i64) {
    match readiness {
        Readiness::Now(spare) => (0, -i64::from(spare)),
        Readiness::Later(Some(at)) => (1, at.timestamp_millis()),
        Readiness::Later(None) => (2, 0),
    }
}
