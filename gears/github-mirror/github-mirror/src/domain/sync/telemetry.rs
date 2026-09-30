use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GithubApi {
    Rest,
    Graphql,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestOutcome {
    Fresh,
    NotModified,
    RateLimited,
    Failed,
}

#[derive(Debug, Default)]
pub struct SessionTelemetry {
    rest_calls: AtomicU64,
    graphql_calls: AtomicU64,
    fresh: AtomicU64,
    not_modified: AtomicU64,
    rate_limited: AtomicU64,
    failed: AtomicU64,
    bytes_downloaded: AtomicU64,
    bytes_saved: AtomicU64,
    rate_limit_waits: AtomicU64,
    rate_limit_wait_ms: AtomicU64,
    graphql_points: AtomicU64,
}

impl SessionTelemetry {
    pub fn count_request(&self, api: GithubApi, outcome: RequestOutcome) {
        let calls = match api {
            GithubApi::Rest => &self.rest_calls,
            GithubApi::Graphql => &self.graphql_calls,
        };
        calls.fetch_add(1, Ordering::Relaxed);
        let answers = match outcome {
            RequestOutcome::Fresh => &self.fresh,
            RequestOutcome::NotModified => &self.not_modified,
            RequestOutcome::RateLimited => &self.rate_limited,
            RequestOutcome::Failed => &self.failed,
        };
        answers.fetch_add(1, Ordering::Relaxed);
    }

    pub fn add_downloaded(&self, bytes: usize) {
        self.bytes_downloaded
            .fetch_add(u64::try_from(bytes).unwrap_or(u64::MAX), Ordering::Relaxed);
    }

    pub fn add_saved(&self, bytes: usize) {
        self.bytes_saved
            .fetch_add(u64::try_from(bytes).unwrap_or(u64::MAX), Ordering::Relaxed);
    }

    pub fn add_rate_limit_wait(&self, waited: Duration) {
        self.rate_limit_waits.fetch_add(1, Ordering::Relaxed);
        self.rate_limit_wait_ms.fetch_add(
            u64::try_from(waited.as_millis()).unwrap_or(u64::MAX),
            Ordering::Relaxed,
        );
    }

    pub fn add_graphql_points(&self, points: u64) {
        self.graphql_points.fetch_add(points, Ordering::Relaxed);
    }

    #[must_use]
    pub fn snapshot(&self) -> TelemetrySnapshot {
        TelemetrySnapshot {
            rest_calls: self.rest_calls.load(Ordering::Relaxed),
            graphql_calls: self.graphql_calls.load(Ordering::Relaxed),
            fresh: self.fresh.load(Ordering::Relaxed),
            not_modified: self.not_modified.load(Ordering::Relaxed),
            rate_limited: self.rate_limited.load(Ordering::Relaxed),
            failed: self.failed.load(Ordering::Relaxed),
            bytes_downloaded: self.bytes_downloaded.load(Ordering::Relaxed),
            bytes_saved: self.bytes_saved.load(Ordering::Relaxed),
            rate_limit_waits: self.rate_limit_waits.load(Ordering::Relaxed),
            rate_limit_wait_ms: self.rate_limit_wait_ms.load(Ordering::Relaxed),
            graphql_points: self.graphql_points.load(Ordering::Relaxed),
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct TelemetrySnapshot {
    pub rest_calls: u64,
    pub graphql_calls: u64,
    pub fresh: u64,
    pub not_modified: u64,
    pub rate_limited: u64,
    pub failed: u64,
    pub bytes_downloaded: u64,
    pub bytes_saved: u64,
    pub rate_limit_waits: u64,
    pub rate_limit_wait_ms: u64,
    pub graphql_points: u64,
}

#[cfg(test)]
#[path = "telemetry_tests.rs"]
mod telemetry_tests;
