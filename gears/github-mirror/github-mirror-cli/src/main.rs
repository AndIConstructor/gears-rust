mod app;
mod registered_gears;

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use authn_resolver_sdk::AuthNResolverClient;
use clap::{Parser, Subcommand};
use figment::Figment;
use figment::providers::Serialized;
use github_mirror::domain::ports::github::ForceMode;
use github_mirror::domain::service::Service;
use serde_json::Value;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;
use toolkit::bootstrap::AppConfig;
use toolkit::runtime::{DbOptions, HostRuntime};
use toolkit::{ClientHub, GearRegistry};
use toolkit_db::DbManager;
use toolkit_security::SecurityContext;
use uuid::Uuid;

use crate::app::commands::{self, Entity};
use crate::app::config;
use crate::app::output::{self, OutputFormat};

#[derive(Parser)]
#[command(
    name = "github-mirror",
    version,
    about = "Mirror GitHub repositories into a local database and query them"
)]
struct Cli {
    #[arg(
        long,
        global = true,
        default_value = "./github-mirror.toml",
        help = "TOML configuration file"
    )]
    config: PathBuf,

    #[arg(
        long,
        global = true,
        env = "GITHUB_TOKEN",
        hide_env_values = true,
        help = "GitHub token; falls back to GITHUB_TOKEN, then ~/.github-mirror/gh_token.txt"
    )]
    token: Option<String>,

    #[arg(
        long,
        global = true,
        env = "CF_PLATFORM_TOKEN",
        hide_env_values = true,
        help = "Platform login token; falls back to CF_PLATFORM_TOKEN, then ~/.github-mirror/platform_token.txt"
    )]
    platform_token: Option<String>,

    #[arg(short, long, global = true, action = clap::ArgAction::Count, help = "More log output (-v, -vv, -vvv)")]
    verbose: u8,

    #[arg(
        long,
        global = true,
        help = "Log filter, e.g. info or github_mirror=debug"
    )]
    log_level: Option<String>,

    #[arg(short, long, global = true, help = "Only errors in the log output")]
    quiet: bool,

    #[arg(
        long,
        global = true,
        value_enum,
        help = "table (default for sync and status) or json (default for query)"
    )]
    output_format: Option<OutputFormat>,

    #[arg(long, help = "Print the built-in configuration template and exit")]
    print_config: bool,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    #[command(about = "Synchronize a repository")]
    Sync {
        #[arg(help = "ORG/REPO")]
        repo: String,
        #[arg(
            long,
            help = "Walk every listing and refine every entity, keeping the HTTP cache"
        )]
        force_full: bool,
        #[arg(long, help = "Like --force-full, and also bypass the HTTP cache")]
        force: bool,
    },
    #[command(about = "Continue an interrupted synchronization")]
    Resume {
        #[arg(help = "ORG/REPO")]
        repo: String,
        #[arg(
            long,
            help = "Walk every listing and refine every entity, keeping the HTTP cache"
        )]
        force_full: bool,
        #[arg(long, help = "Like --force-full, and also bypass the HTTP cache")]
        force: bool,
    },
    #[command(about = "Read mirrored data from the local database")]
    Query {
        #[arg(value_enum)]
        entity: Entity,
        #[arg(help = "ORG/REPO")]
        repo: String,
        #[arg(
            long,
            help = "Issue or pull request number, for entities that belong to one"
        )]
        number: Option<i64>,
        #[arg(long, default_value_t = 30, help = "Most rows to return")]
        limit: u64,
    },
    #[command(about = "Show the synchronization status of a repository")]
    Status {
        #[arg(help = "ORG/REPO")]
        repo: String,
    },
    #[command(
        name = "clear-cache",
        about = "Remove everything mirrored for a repository: its data, change-detection state and cached responses"
    )]
    ClearCache {
        #[arg(help = "ORG/REPO")]
        repo: String,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    init_logging(&cli);
    match run(cli).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

async fn run(cli: Cli) -> Result<()> {
    if cli.print_config {
        print!("{}", config::DEFAULT_TEMPLATE);
        return Ok(());
    }
    let Some(command) = cli.command else {
        bail!("no command given; see --help");
    };

    let platform_token = config::resolve_token(cli.platform_token, "platform_token.txt").ok_or_else(|| {
        anyhow!(
            "no platform token: pass --platform-token, set CF_PLATFORM_TOKEN or write ~/.github-mirror/platform_token.txt"
        )
    })?;
    let github_token = config::resolve_token(cli.token, "gh_token.txt");
    let loaded = config::load(&cli.config, github_token.as_deref())?;
    let format = cli.output_format.unwrap_or(match command {
        Command::Query { .. } => OutputFormat::Json,
        Command::Sync { .. }
        | Command::Resume { .. }
        | Command::Status { .. }
        | Command::ClearCache { .. } => OutputFormat::Table,
    });

    let runtime = Runtime::start(loaded.app)?;
    let outcome = execute(&runtime, command, &platform_token, loaded.tenant_id).await;
    let stopped = runtime.stop().await;
    let value = match (outcome, stopped) {
        (Ok(value), Ok(())) => value,
        (Err(e), Ok(())) | (_, Err(e)) => return Err(e),
    };
    output::print(&value, format)
}

async fn execute(
    runtime: &Runtime,
    command: Command,
    platform_token: &str,
    tenant_id: Option<Uuid>,
) -> Result<Value> {
    let ctx = runtime.authenticate(platform_token).await?;
    if let Some(expected) = tenant_id
        && expected != ctx.subject_tenant_id()
    {
        bail!(
            "cli.tenant_id is {expected}, but the platform token belongs to tenant {}",
            ctx.subject_tenant_id()
        );
    }
    let service = runtime.service().await?;
    match command {
        Command::Sync {
            repo,
            force_full,
            force,
        }
        | Command::Resume {
            repo,
            force_full,
            force,
        } => {
            commands::sync(
                &service,
                &ctx,
                &repo,
                ForceMode::from_flags(force, force_full),
            )
            .await
        }
        Command::Query {
            entity,
            repo,
            number,
            limit,
        } => commands::query(&service, &ctx, entity, &repo, number, limit).await,
        Command::Status { repo } => commands::status(&service, &ctx, &repo).await,
        Command::ClearCache { repo } => commands::clear_cache(&service, &ctx, &repo).await,
    }
}

fn init_logging(cli: &Cli) {
    let level = cli.log_level.clone().unwrap_or_else(|| {
        let preset = match (cli.quiet, cli.verbose) {
            (true, _) => "error",
            (false, 0) => "warn",
            (false, 1) => "info",
            (false, 2) => "debug",
            (false, _) => "trace",
        };
        preset.to_owned()
    });
    let filter = tracing_subscriber::EnvFilter::try_new(&level)
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .init();
}

const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const POLL_EVERY: Duration = Duration::from_millis(50);

struct Runtime {
    hub: Arc<ClientHub>,
    cancel: CancellationToken,
    phases: JoinHandle<Result<()>>,
}

impl Runtime {
    fn start(config: AppConfig) -> Result<Self> {
        toolkit::bootstrap::init_crypto_provider()?;
        let db = database(&config)?;
        let hub = Arc::new(ClientHub::default());
        let cancel = CancellationToken::new();
        let host = HostRuntime::new(
            GearRegistry::discover_and_build()?,
            Arc::new(config),
            db,
            Arc::clone(&hub),
            cancel.clone(),
            Uuid::new_v4(),
            None,
        );
        let phases = tokio::spawn(host.run_gear_phases());
        Ok(Self {
            hub,
            cancel,
            phases,
        })
    }

    async fn authenticate(&self, platform_token: &str) -> Result<SecurityContext> {
        let authn = self
            .wait_for(ClientHub::try_get::<dyn AuthNResolverClient>)
            .await?;
        let result = authn
            .authenticate(platform_token)
            .await
            .map_err(|e| anyhow!("the platform token was refused: {e}"))?;
        Ok(result.security_context)
    }

    async fn service(&self) -> Result<Arc<Service>> {
        self.wait_for(|hub| hub.try_get::<Service>().filter(|service| service.started()))
            .await
    }

    async fn stop(self) -> Result<()> {
        self.cancel.cancel();
        self.phases
            .await
            .context("the gear runtime task failed")?
            .context("the gear runtime failed")
    }

    async fn wait_for<T>(&self, find: impl Fn(&ClientHub) -> Option<Arc<T>>) -> Result<Arc<T>>
    where
        T: ?Sized + Send + Sync + 'static,
    {
        let deadline = tokio::time::Instant::now() + STARTUP_TIMEOUT;
        loop {
            if let Some(found) = find(&self.hub) {
                return Ok(found);
            }
            if self.phases.is_finished() {
                bail!("the gear runtime stopped before it was ready");
            }
            if tokio::time::Instant::now() >= deadline {
                bail!(
                    "the gear runtime was not ready after {} seconds",
                    STARTUP_TIMEOUT.as_secs()
                );
            }
            tokio::time::sleep(POLL_EVERY).await;
        }
    }
}

fn database(config: &AppConfig) -> Result<DbOptions> {
    if config.database.is_none() {
        return Ok(DbOptions::None);
    }
    let figment = Figment::new().merge(Serialized::defaults(config));
    let manager = DbManager::from_figment(figment, config.server.home_dir.clone())?;
    Ok(DbOptions::Manager(Arc::new(manager)))
}
