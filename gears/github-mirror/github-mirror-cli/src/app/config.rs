use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use figment::Figment;
use figment::providers::Serialized;
use serde_json::{Value, json};
use toolkit::bootstrap::AppConfig;
use uuid::Uuid;

pub const DEFAULT_TEMPLATE: &str = include_str!("../default_config.toml");

pub struct CliConfig {
    pub app: AppConfig,
    pub tenant_id: Option<Uuid>,
}

pub fn load(path: &Path, github_token: Option<&str>) -> Result<CliConfig> {
    let mut merged: Value =
        toml::from_str(DEFAULT_TEMPLATE).context("the built-in configuration does not parse")?;
    if path.is_file() {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("cannot read {}", path.display()))?;
        let overrides: Value = toml::from_str(&text)
            .with_context(|| format!("{} is not valid TOML", path.display()))?;
        merge(&mut merged, overrides);
    }

    let tenant_id = take_cli_tenant(&mut merged)?;
    match github_token {
        Some(token) => add_github_token(&mut merged, token)?,
        None => drop_github_token(&mut merged),
    }
    expand_home_dir(&mut merged)?;

    let app: AppConfig = Figment::new()
        .merge(Serialized::defaults(AppConfig::default()))
        .merge(Serialized::defaults(merged))
        .extract()
        .context("the configuration does not match the runtime's settings")?;
    Ok(CliConfig { app, tenant_id })
}

pub fn resolve_token(given: Option<String>, file_name: &str) -> Option<String> {
    if let Some(token) = given.map(|token| token.trim().to_owned()).filter(|token| !token.is_empty()) {
        return Some(token);
    }
    let path = user_home().ok()?.join(".github-mirror").join(file_name);
    std::fs::read_to_string(path)
        .ok()
        .map(|text| text.trim().to_owned())
        .filter(|token| !token.is_empty())
}

fn merge(base: &mut Value, overrides: Value) {
    match (base, overrides) {
        (Value::Object(base), Value::Object(overrides)) => {
            for (key, value) in overrides {
                match base.get_mut(&key) {
                    Some(slot) => merge(slot, value),
                    None => {
                        base.insert(key, value);
                    }
                }
            }
        }
        (base, overrides) => *base = overrides,
    }
}

fn take_cli_tenant(config: &mut Value) -> Result<Option<Uuid>> {
    let Some(cli) = config.as_object_mut().and_then(|root| root.remove("cli")) else {
        return Ok(None);
    };
    cli.get("tenant_id")
        .map(|value| {
            let text = value
                .as_str()
                .ok_or_else(|| anyhow!("cli.tenant_id must be a string"))?;
            Uuid::parse_str(text).context("cli.tenant_id is not a UUID")
        })
        .transpose()
}

fn add_github_token(config: &mut Value, token: &str) -> Result<()> {
    let secret = config
        .pointer("/gears/github-mirror/config/github_token_secret")
        .cloned()
        .ok_or_else(|| anyhow!("gears.github-mirror.config.github_token_secret is missing"))?;
    let entry = json!({
        "tenant_id": secret.get("tenant_id"),
        "key": secret.get("key"),
        "value": token,
    });
    config
        .pointer_mut("/gears/static-credstore-plugin/config/secrets")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| anyhow!("gears.static-credstore-plugin.config.secrets must be a list"))?
        .push(entry);
    Ok(())
}

fn drop_github_token(config: &mut Value) {
    if let Some(gear) = config
        .pointer_mut("/gears/github-mirror/config")
        .and_then(Value::as_object_mut)
    {
        gear.remove("github_token_secret");
    }
}

fn expand_home_dir(config: &mut Value) -> Result<()> {
    let Some(home_dir) = config.pointer_mut("/server/home_dir") else {
        return Ok(());
    };
    let Some(raw) = home_dir.as_str() else {
        bail!("server.home_dir must be a string");
    };
    let path = match raw.strip_prefix("~/") {
        Some(rest) => user_home()?.join(rest),
        None if raw == "~" => user_home()?,
        None => PathBuf::from(raw),
    };
    std::fs::create_dir_all(&path)
        .with_context(|| format!("cannot create {}", path.display()))?;
    *home_dir = Value::String(path.to_string_lossy().into_owned());
    Ok(())
}

fn user_home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| anyhow!("cannot find the home directory: neither HOME nor USERPROFILE is set"))
}
