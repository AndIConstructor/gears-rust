use std::time::Duration;

use super::{GithubApi, RequestOutcome, SessionTelemetry, TelemetrySnapshot};

#[test]
fn each_request_is_counted_by_api_and_by_outcome() {
    let telemetry = SessionTelemetry::default();

    telemetry.count_request(GithubApi::Rest, RequestOutcome::Fresh);
    telemetry.count_request(GithubApi::Rest, RequestOutcome::NotModified);
    telemetry.count_request(GithubApi::Rest, RequestOutcome::NotModified);
    telemetry.count_request(GithubApi::Rest, RequestOutcome::RateLimited);
    telemetry.count_request(GithubApi::Graphql, RequestOutcome::Fresh);
    telemetry.count_request(GithubApi::Graphql, RequestOutcome::Failed);

    assert_eq!(
        telemetry.snapshot(),
        TelemetrySnapshot {
            rest_calls: 4,
            graphql_calls: 2,
            fresh: 2,
            not_modified: 2,
            rate_limited: 1,
            failed: 1,
            ..TelemetrySnapshot::default()
        }
    );
}

#[test]
fn bytes_waits_and_points_add_up() {
    let telemetry = SessionTelemetry::default();

    telemetry.add_downloaded(1_000);
    telemetry.add_downloaded(500);
    telemetry.add_saved(2_048);
    telemetry.add_rate_limit_wait(Duration::from_millis(1_500));
    telemetry.add_rate_limit_wait(Duration::from_millis(250));
    telemetry.add_graphql_points(1);
    telemetry.add_graphql_points(3);

    assert_eq!(
        telemetry.snapshot(),
        TelemetrySnapshot {
            bytes_downloaded: 1_500,
            bytes_saved: 2_048,
            rate_limit_waits: 2,
            rate_limit_wait_ms: 1_750,
            graphql_points: 4,
            ..TelemetrySnapshot::default()
        }
    );
}

#[test]
fn requests_counted_from_many_threads_are_all_kept() {
    let telemetry = SessionTelemetry::default();

    std::thread::scope(|scope| {
        for _ in 0..8 {
            scope.spawn(|| {
                for _ in 0..1_000 {
                    telemetry.count_request(GithubApi::Rest, RequestOutcome::NotModified);
                    telemetry.add_saved(10);
                }
            });
        }
    });

    let snapshot = telemetry.snapshot();
    assert_eq!(snapshot.rest_calls, 8_000);
    assert_eq!(snapshot.not_modified, 8_000);
    assert_eq!(snapshot.bytes_saved, 80_000);
}
