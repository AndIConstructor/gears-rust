use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct SettingsConfig {
    #[serde(default = "default_max_field_length")]
    pub max_field_length: usize,

    /// How many named settings one user may hold in one tenant.
    #[serde(default = "default_named_settings_per_user")]
    pub named_settings_per_user: usize,

    /// Upper bound on one named setting's value, as serialized JSON bytes.
    #[serde(default = "default_named_value_max_bytes")]
    pub named_value_max_bytes: usize,
}

impl Default for SettingsConfig {
    fn default() -> Self {
        Self {
            max_field_length: default_max_field_length(),
            named_settings_per_user: default_named_settings_per_user(),
            named_value_max_bytes: default_named_value_max_bytes(),
        }
    }
}

fn default_max_field_length() -> usize {
    100
}

fn default_named_settings_per_user() -> usize {
    256
}

fn default_named_value_max_bytes() -> usize {
    4096
}

/// Upper bound on `named_value_max_bytes`: the capacity of `MySQL`'s `TEXT`, the
/// smallest column any supported backend stores the value in.
pub const MAX_NAMED_VALUE_BYTES: usize = 65_535;

/// Upper bound on `named_settings_per_user`. The list endpoint returns every key
/// of a user in one response, unpaged, so the count times the value bound is
/// what one response can carry: 4096 keys at the default 4 KiB is 16 MiB. This
/// is a store for small UI preferences, and 16 times the default key count is
/// room enough for that.
pub const MAX_NAMED_SETTINGS_PER_USER: usize = 4096;

impl SettingsConfig {
    /// Reject limits that would break the gear at request time rather than at
    /// startup, where a typo is cheap to notice.
    ///
    /// # Errors
    ///
    /// When `named_settings_per_user` is 0 (every new key refused) or above
    /// [`MAX_NAMED_SETTINGS_PER_USER`], or `named_value_max_bytes` is 0 or
    /// above [`MAX_NAMED_VALUE_BYTES`].
    pub fn validate(&self) -> anyhow::Result<()> {
        if self.named_settings_per_user == 0
            || self.named_settings_per_user > MAX_NAMED_SETTINGS_PER_USER
        {
            anyhow::bail!(
                "simple-user-settings: named_settings_per_user must be between 1 and \
                 {MAX_NAMED_SETTINGS_PER_USER}, got {}",
                self.named_settings_per_user
            );
        }
        if self.named_value_max_bytes == 0 || self.named_value_max_bytes > MAX_NAMED_VALUE_BYTES {
            anyhow::bail!(
                "simple-user-settings: named_value_max_bytes must be between 1 and \
                 {MAX_NAMED_VALUE_BYTES}, got {}",
                self.named_value_max_bytes
            );
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_defaults_are_valid() {
        SettingsConfig::default().validate().expect("defaults");
    }

    #[test]
    fn degenerate_named_limits_are_refused() {
        let with = |per_user, max_bytes| SettingsConfig {
            named_settings_per_user: per_user,
            named_value_max_bytes: max_bytes,
            ..SettingsConfig::default()
        };
        assert!(with(0, 4096).validate().is_err(), "no keys at all");
        assert!(
            with(MAX_NAMED_SETTINGS_PER_USER + 1, 4096)
                .validate()
                .is_err(),
            "more keys than one unpaged list should carry"
        );
        assert!(with(256, 0).validate().is_err(), "no value fits");
        assert!(
            with(256, MAX_NAMED_VALUE_BYTES + 1).validate().is_err(),
            "past TEXT"
        );
        with(1, 1).validate().expect("smallest usable");
        with(MAX_NAMED_SETTINGS_PER_USER, MAX_NAMED_VALUE_BYTES)
            .validate()
            .expect("largest allowed");
    }
}
