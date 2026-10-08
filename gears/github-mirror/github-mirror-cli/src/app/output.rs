use anyhow::Result;
use serde_json::{Map, Value};

const MAX_CELL: usize = 60;
const SECTION_ORDER: [&str; 5] = ["repository", "run", "objects", "api", "storage"];

#[derive(Clone, Copy, clap::ValueEnum)]
pub enum OutputFormat {
    Table,
    Json,
}

pub fn print(value: &Value, format: OutputFormat) -> Result<()> {
    match format {
        OutputFormat::Json => println!("{}", serde_json::to_string_pretty(value)?),
        OutputFormat::Table => print!("{}", table(value)),
    }
    Ok(())
}

fn table(value: &Value) -> String {
    match value {
        Value::Array(rows) => rows_table(rows),
        Value::Object(sections)
            if !sections.is_empty() && sections.values().all(Value::is_object) =>
        {
            let mut ordered: Vec<(&String, &Value)> = sections.iter().collect();
            ordered.sort_by_key(|(title, _)| {
                SECTION_ORDER
                    .iter()
                    .position(|known| known == title)
                    .unwrap_or(SECTION_ORDER.len())
            });
            let mut out = String::new();
            for (title, section) in ordered {
                out.push_str(&title.to_uppercase());
                out.push('\n');
                if section.as_object().is_some_and(Map::is_empty) {
                    out.push_str("(none)\n");
                } else {
                    out.push_str(&table(section));
                }
                out.push('\n');
            }
            out
        }
        Value::Object(fields) => {
            let mut pairs = Vec::new();
            flatten("", fields, &mut pairs);
            let rows: Vec<Vec<String>> = pairs
                .into_iter()
                .map(|(key, value)| vec![key, value])
                .collect();
            render(&["field".to_owned(), "value".to_owned()], &rows)
        }
        other => format!("{}\n", cell(other)),
    }
}

fn rows_table(rows: &[Value]) -> String {
    let mut columns: Vec<String> = Vec::new();
    for row in rows.iter().filter_map(Value::as_object) {
        for (key, value) in row {
            if !matches!(value, Value::Object(_) | Value::Array(_)) && !columns.contains(key) {
                columns.push(key.clone());
            }
        }
    }
    let cells: Vec<Vec<String>> = rows
        .iter()
        .map(|row| {
            columns
                .iter()
                .map(|column| row.get(column).map_or_else(String::new, cell))
                .collect()
        })
        .collect();
    render(&columns, &cells)
}

fn flatten(prefix: &str, fields: &Map<String, Value>, out: &mut Vec<(String, String)>) {
    for (key, value) in fields {
        let path = if prefix.is_empty() {
            key.clone()
        } else {
            format!("{prefix}.{key}")
        };
        match value {
            Value::Object(nested) => flatten(&path, nested, out),
            other => out.push((path, cell(other))),
        }
    }
}

fn cell(value: &Value) -> String {
    let text = match value {
        Value::String(text) => text.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    };
    let single_line = text.replace(['\n', '\r'], " ");
    if single_line.chars().count() > MAX_CELL {
        let cut: String = single_line.chars().take(MAX_CELL - 3).collect();
        format!("{cut}...")
    } else {
        single_line
    }
}

fn render(header: &[String], rows: &[Vec<String>]) -> String {
    let widths: Vec<usize> = header
        .iter()
        .enumerate()
        .map(|(index, title)| {
            rows.iter()
                .filter_map(|row| row.get(index))
                .map(|text| text.chars().count())
                .chain(std::iter::once(title.chars().count()))
                .max()
                .unwrap_or_default()
        })
        .collect();
    let mut out = String::new();
    line(&mut out, header, &widths);
    let rule: Vec<String> = widths.iter().map(|width| "-".repeat(*width)).collect();
    line(&mut out, &rule, &widths);
    for row in rows {
        line(&mut out, row, &widths);
    }
    out
}

fn line(out: &mut String, cells: &[String], widths: &[usize]) {
    let padded: Vec<String> = cells
        .iter()
        .zip(widths)
        .map(|(text, &width)| format!("{text:<width$}"))
        .collect();
    out.push_str(padded.join("  ").trim_end());
    out.push('\n');
}
