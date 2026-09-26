use serde::{Deserialize, Serialize};

/// Orphan watchdog — detects and finalizes turns abandoned by crashed pods.
///
/// Requires leader election (exactly one active instance per environment).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrphanWatchdogConfig {
    /// Enable the orphan watchdog. Default: `true`.
    #[serde(default = "super::default_true")]
    pub enabled: bool,
    /// Scan interval in seconds. Default: 60.
    #[serde(default = "default_orphan_scan_interval")]
    pub scan_interval_secs: u64,
    /// A `running` turn with `last_progress_at` older than this is orphan-eligible.
    /// Valid range: 90–3600. Default: 300 (5 min).
    /// Minimum 90s = 3× `PROGRESS_UPDATE_INTERVAL` (30s) to avoid false orphaning.
    #[serde(default = "default_orphan_timeout")]
    pub timeout_secs: u64,
}

impl Default for OrphanWatchdogConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            scan_interval_secs: default_orphan_scan_interval(),
            timeout_secs: default_orphan_timeout(),
        }
    }
}

impl OrphanWatchdogConfig {
    /// Minimum timeout to avoid false orphaning under normal jitter.
    /// `PROGRESS_UPDATE_INTERVAL` is 30s; 90s gives 3 heartbeat windows of headroom.
    const MIN_TIMEOUT_SECS: u64 = 90;

    pub fn validate(&self) -> Result<(), String> {
        if !(Self::MIN_TIMEOUT_SECS..=3600).contains(&self.timeout_secs) {
            return Err(format!(
                "orphan_watchdog.timeout_secs must be {}-3600, got {}",
                Self::MIN_TIMEOUT_SECS,
                self.timeout_secs
            ));
        }
        if self.scan_interval_secs == 0 {
            return Err("orphan_watchdog.scan_interval_secs must be > 0".to_owned());
        }
        Ok(())
    }
}

fn default_orphan_scan_interval() -> u64 {
    60
}
fn default_orphan_timeout() -> u64 {
    300
}

/// Thread summary background worker — claims and executes pending thread
/// summary tasks. Requires leader election.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ThreadSummaryWorkerConfig {
    /// Enable the thread summary worker. Default: `true`.
    #[serde(default = "super::default_true")]
    pub enabled: bool,
    /// Maximum interval between reconciliation scans. Default: 60s.
    #[serde(default = "default_ts_reconcile_interval")]
    pub reconcile_interval_secs: u64,
    /// Outbox lease for one summary task: the handler is cancelled and the
    /// task redelivered after this. Must be at least 5s. Default: 300s.
    #[serde(default = "default_ts_claim_timeout")]
    pub claim_timeout_secs: u64,
    /// Attempts per task before it is dead-lettered. Default: 3.
    #[serde(default = "default_ts_max_attempts")]
    pub max_attempts: u32,
    /// Compression threshold: summary triggered when estimated input tokens
    /// reach this percentage of the effective input token budget. Default: 80.
    #[serde(default = "default_compression_threshold")]
    pub compression_threshold_pct: u32,
    /// Model ID from the model catalog for summary generation.
    /// Empty string falls back to `gpt-4.1-mini`. Default: empty.
    #[serde(default)]
    pub summary_model_id: String,
    /// Fallback system prompt when `ModelCatalogEntry.thread_summary_prompt` is empty.
    #[serde(default = "default_summary_system_prompt")]
    pub summary_system_prompt: String,
    /// Maximum characters per message included in the summary prompt.
    /// Messages longer than this are truncated with "..." appended.
    /// 0 = no truncation. Default: 4000.
    #[serde(default = "default_message_content_limit")]
    pub message_content_limit: usize,
}

impl Default for ThreadSummaryWorkerConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            reconcile_interval_secs: default_ts_reconcile_interval(),
            claim_timeout_secs: default_ts_claim_timeout(),
            max_attempts: default_ts_max_attempts(),
            compression_threshold_pct: default_compression_threshold(),
            summary_model_id: String::new(),
            summary_system_prompt: default_summary_system_prompt(),
            message_content_limit: default_message_content_limit(),
        }
    }
}

impl ThreadSummaryWorkerConfig {
    pub fn validate(&self) -> Result<(), String> {
        if self.reconcile_interval_secs == 0 {
            return Err("thread_summary_worker.reconcile_interval_secs must be > 0".to_owned());
        }
        if self.claim_timeout_secs < 5 {
            return Err("thread_summary_worker.claim_timeout_secs must be >= 5".to_owned());
        }
        if self.max_attempts == 0 {
            return Err("thread_summary_worker.max_attempts must be > 0".to_owned());
        }
        if self.compression_threshold_pct == 0 || self.compression_threshold_pct > 99 {
            return Err(format!(
                "thread_summary_worker.compression_threshold_pct must be 1-99, got {}",
                self.compression_threshold_pct
            ));
        }
        Ok(())
    }
}

fn default_ts_reconcile_interval() -> u64 {
    60
}
fn default_ts_claim_timeout() -> u64 {
    300
}
fn default_ts_max_attempts() -> u32 {
    3
}
fn default_compression_threshold() -> u32 {
    80
}
fn default_summary_system_prompt() -> String {
    "You are a conversation summarizer. Given a conversation (and optionally an existing \
     summary), produce a detailed structured summary. Respond with an <analysis> block \
     (your reasoning) followed by a <summary> block (the final summary). Only the \
     <summary> content will be stored. Do not invent information not present in the \
     conversation."
        .to_owned()
}
fn default_message_content_limit() -> usize {
    4000
}

/// Cleanup worker — removes provider resources for soft-deleted chats.
///
/// Target design: this worker does not require leader election because row
/// claiming is intended to be concurrent-safe via `SELECT … FOR UPDATE SKIP LOCKED`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CleanupWorkerConfig {
    /// Enable the cleanup worker. Default: `true`.
    #[serde(default = "super::default_true")]
    pub enabled: bool,
    /// Poll interval in seconds. Default: 60.
    #[serde(default = "default_cleanup_poll_interval")]
    pub poll_interval_secs: u64,
    /// Maximum interval between reconciliation scans for stuck rows. Default: 300s.
    #[serde(default = "default_cleanup_reconcile_interval")]
    pub reconcile_interval_secs: u64,
    /// Timeout for stale `in_progress` rows. Default: 900s.
    #[serde(default = "default_cleanup_stale_timeout")]
    pub stale_in_progress_timeout_secs: u64,
    /// Max attachments claimed per poll cycle. Default: 32.
    #[serde(default = "default_cleanup_batch_size")]
    pub batch_size: u32,
    /// Max retry attempts per attachment. Default: 5.
    #[serde(default = "default_cleanup_max_attempts")]
    pub max_attempts: u32,
}

impl Default for CleanupWorkerConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            poll_interval_secs: default_cleanup_poll_interval(),
            reconcile_interval_secs: default_cleanup_reconcile_interval(),
            stale_in_progress_timeout_secs: default_cleanup_stale_timeout(),
            batch_size: default_cleanup_batch_size(),
            max_attempts: default_cleanup_max_attempts(),
        }
    }
}

impl CleanupWorkerConfig {
    pub fn validate(&self) -> Result<(), String> {
        if self.poll_interval_secs == 0 {
            return Err("cleanup_worker.poll_interval_secs must be > 0".to_owned());
        }
        if self.reconcile_interval_secs == 0 {
            return Err("cleanup_worker.reconcile_interval_secs must be > 0".to_owned());
        }
        if self.batch_size == 0 {
            return Err("cleanup_worker.batch_size must be > 0".to_owned());
        }
        if self.max_attempts == 0 {
            return Err("cleanup_worker.max_attempts must be > 0".to_owned());
        }
        if self.stale_in_progress_timeout_secs == 0 {
            return Err("cleanup_worker.stale_in_progress_timeout_secs must be > 0".to_owned());
        }
        Ok(())
    }
}

fn default_cleanup_poll_interval() -> u64 {
    60
}
fn default_cleanup_reconcile_interval() -> u64 {
    300
}
fn default_cleanup_stale_timeout() -> u64 {
    900
}
fn default_cleanup_batch_size() -> u32 {
    32
}
fn default_cleanup_max_attempts() -> u32 {
    5
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_worker_configs_are_valid() {
        OrphanWatchdogConfig::default().validate().unwrap();
        ThreadSummaryWorkerConfig::default().validate().unwrap();
        CleanupWorkerConfig::default().validate().unwrap();
    }
}
