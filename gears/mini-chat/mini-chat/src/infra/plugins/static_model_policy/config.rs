use mini_chat_sdk::{KillSwitches, ModelCatalogEntry, TierLimits};
use serde::Deserialize;

/// Plugin configuration.
///
/// `model_catalog` key is required during deserialization (no `#[serde(default)]`),
/// but an empty list is valid — the plugin operates with zero models.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StaticMiniChatPolicyPluginConfig {
    /// Vendor name for GTS instance registration.
    #[serde(default = "default_vendor")]
    pub vendor: String,

    /// Plugin priority (lower = higher priority).
    #[serde(default = "default_priority")]
    pub priority: i16,

    /// Static model catalog entries.
    pub model_catalog: Vec<ModelCatalogEntry>,

    /// Static kill switches (all disabled by default).
    #[serde(default)]
    pub kill_switches: KillSwitches,

    /// Static per-user tier limits (used for all users).
    #[serde(default = "default_standard_limits")]
    pub default_standard_limits: TierLimits,
    #[serde(default = "default_premium_limits")]
    pub default_premium_limits: TierLimits,
}

impl StaticMiniChatPolicyPluginConfig {
    /// Every catalog entry must price usage: both credit multipliers in
    /// `1..=MAX_MULT` (a zero multiplier would make usage free).
    pub fn validate(&self) -> Result<(), String> {
        use crate::domain::service::credit_arithmetic::MAX_MULT;
        for entry in &self.model_catalog {
            for (name, value) in [
                (
                    "input_tokens_credit_multiplier_micro",
                    entry.input_tokens_credit_multiplier_micro,
                ),
                (
                    "output_tokens_credit_multiplier_micro",
                    entry.output_tokens_credit_multiplier_micro,
                ),
            ] {
                if value == 0 || value > MAX_MULT {
                    return Err(format!(
                        "model '{}': {name} must be 1..={MAX_MULT}, got {value}",
                        entry.id
                    ));
                }
            }
        }
        Ok(())
    }
}

impl Default for StaticMiniChatPolicyPluginConfig {
    fn default() -> Self {
        Self {
            vendor: default_vendor(),
            priority: default_priority(),
            model_catalog: Vec::new(),
            kill_switches: KillSwitches::default(),
            default_standard_limits: default_standard_limits(),
            default_premium_limits: default_premium_limits(),
        }
    }
}

fn default_vendor() -> String {
    "constructorfabric".to_owned()
}

const fn default_priority() -> i16 {
    100
}

fn default_standard_limits() -> TierLimits {
    TierLimits {
        limit_daily_credits_micro: 100_000_000,
        limit_monthly_credits_micro: 1_000_000_000,
    }
}

fn default_premium_limits() -> TierLimits {
    TierLimits {
        limit_daily_credits_micro: 50_000_000,
        limit_monthly_credits_micro: 500_000_000,
    }
}
