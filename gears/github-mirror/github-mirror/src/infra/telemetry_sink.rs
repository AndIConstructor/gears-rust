use std::fs::OpenOptions;
use std::io::Write as _;
use std::path::Path;

use crate::domain::error::DomainError;
use crate::domain::ports::telemetry_sink::TelemetrySink;
use crate::domain::sync::TelemetryLine;

#[derive(Debug, Default)]
pub struct JsonlTelemetrySink;

impl TelemetrySink for JsonlTelemetrySink {
    fn record(&self, file: &Path, line: &TelemetryLine<'_>) -> Result<(), DomainError> {
        if let Some(dir) = file.parent() {
            std::fs::create_dir_all(dir).map_err(|e| {
                DomainError::internal(format!("telemetry directory {}: {e}", dir.display()))
            })?;
        }

        let mut text = serde_json::to_string(line)
            .map_err(|e| DomainError::internal(format!("telemetry line: {e}")))?;
        text.push('\n');

        OpenOptions::new()
            .create(true)
            .append(true)
            .open(file)
            .and_then(|mut handle| handle.write_all(text.as_bytes()))
            .map_err(|e| DomainError::internal(format!("telemetry file {}: {e}", file.display())))
    }
}
