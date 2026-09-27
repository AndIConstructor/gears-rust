//! Infrastructure: the built-in `PostgreSQL` implementations of the plugin
//! contracts, the `SeaORM` entities behind them, and the migrations.
//!
//! The built-in store and engine are plugins like any external one — they are
//! packaged here for convenience, not privilege. No domain service reaches an
//! entity, a statement or a connection: the only way out of `domain/` is
//! through the ports in `graph_storage_sdk::plugin_api`.

pub mod embedding;
pub mod engine;
/// The in-memory double, behind `test-support` so it never reaches a release
/// artifact. It exists to make the conformance suite run against a second
/// implementation -- a change only the built-in store can satisfy fails on it
/// -- which is a test concern, not a shipped one.
#[cfg(any(test, feature = "test-support"))]
pub mod fake_store;
pub(crate) mod projections;
pub mod storage;
pub mod store;

/// The longest rendering of a dependency's error that reaches a log line.
const LOGGED_MAX_CHARS: usize = 512;

/// A dependency's error, as it may be logged: at most [`LOGGED_MAX_CHARS`]
/// characters, control characters escaped.
///
/// A driver's or server's diagnostic is neither bounded nor free of control
/// characters, and the platform's default console format is text
/// (`ConsoleFormat::Text`), which writes a field's `Display` as it is -- so
/// an unbounded `%error` could split a line or fill a log. Only the JSON
/// console format and the file sink escape on their own, and a gear cannot
/// assume its deployment chose them.
pub(crate) fn logged(error: &dyn std::fmt::Display) -> String {
    let text = error.to_string();
    let mut out = String::with_capacity(text.len().min(LOGGED_MAX_CHARS) + 16);
    for (taken, c) in text.chars().enumerate() {
        if taken == LOGGED_MAX_CHARS {
            out.push_str(" [truncated]");
            break;
        }
        if c.is_control() {
            out.extend(c.escape_default());
        } else {
            out.push(c);
        }
    }
    out
}

#[cfg(test)]
mod logged_tests {
    use super::{LOGGED_MAX_CHARS, logged};

    /// A newline in a dependency's text does not start a new log record.
    #[test]
    fn control_characters_are_escaped() {
        let text = logged(&"relation \"kb\" does not exist\nERROR forged line");
        assert!(!text.contains('\n'), "{text}");
        assert!(text.contains("\\n"), "{text}");
    }

    /// A diagnostic longer than the bound is cut, and says so.
    #[test]
    fn long_text_is_bounded() {
        let text = logged(&"x".repeat(LOGGED_MAX_CHARS * 4));
        assert!(
            text.chars().count() < LOGGED_MAX_CHARS + 20,
            "{}",
            text.len()
        );
        assert!(text.ends_with("[truncated]"), "{text}");
    }
}
