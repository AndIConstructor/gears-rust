use std::sync::Arc;

use toolkit_utils::SecretString;

use super::controller::RateLimitController;
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
}
