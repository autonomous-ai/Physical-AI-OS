//! Append-only attempt ledger. A row is written before each stimulus; nothing
//! is rewritten or deleted, and a planned attempt without a finished row is
//! reported as incomplete rather than dropped.
use crate::{Result, invalid};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub const LEDGER_FILE: &str = "ledger.jsonl";
const MAX_LEDGER_BYTES: u64 = 512 * 1024 * 1024;

pub struct Ledger {
    file: File,
    path: PathBuf,
    sequence: u64,
}

/// Create a fresh private run directory; an existing path is never reused.
pub fn create_run_directory(path: &Path) -> Result<()> {
    fs::DirBuilder::new().mode(0o700).create(path)?;
    Ok(())
}

pub fn new_private_file(path: &Path) -> Result<File> {
    Ok(OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?)
}

pub fn write_new_json(path: &Path, value: &Value) -> Result<()> {
    let mut file = new_private_file(path)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

pub fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64)
}

impl Ledger {
    pub fn create(run_directory: &Path) -> Result<Self> {
        let path = run_directory.join(LEDGER_FILE);
        Ok(Self {
            file: new_private_file(&path)?,
            path,
            sequence: 0,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Append one row. `durable` rows (attempt boundaries) are synced before
    /// returning, so a crash cannot erase the record of a started attempt.
    pub fn append(&mut self, row: &str, mut body: Value, durable: bool) -> Result<()> {
        self.sequence += 1;
        if !body.is_object() {
            return Err(invalid("ledger rows must be JSON objects"));
        }
        body["row"] = json!(row);
        body["ledger_sequence"] = json!(self.sequence);
        body["written_unix_ms"] = json!(unix_ms());
        serde_json::to_writer(&mut self.file, &body)?;
        self.file.write_all(b"\n")?;
        if durable {
            self.file.sync_data()?;
        }
        Ok(())
    }
}

/// Rows in order, plus whether the final line was truncated (for example by a
/// crash during a write). Any other malformed or missing row is an error.
pub fn read_rows(run_directory: &Path) -> Result<(Vec<Value>, bool)> {
    let path = run_directory.join(LEDGER_FILE);
    if fs::metadata(&path)?.len() > MAX_LEDGER_BYTES {
        return Err(invalid("ledger exceeds 512 MiB"));
    }
    let lines: Vec<String> = BufReader::new(File::open(&path)?)
        .lines()
        .collect::<std::io::Result<_>>()?;
    let lines: Vec<&String> = lines.iter().filter(|l| !l.trim().is_empty()).collect();
    let mut rows = Vec::with_capacity(lines.len());
    for (index, line) in lines.iter().enumerate() {
        let row: Value = match serde_json::from_str(line) {
            Ok(row) => row,
            Err(_) if index + 1 == lines.len() => return Ok((rows, true)),
            Err(error) => return Err(error.into()),
        };
        if row["ledger_sequence"].as_u64() != Some(index as u64 + 1) {
            return Err(invalid("ledger sequence gap: rows were lost or reordered"));
        }
        rows.push(row);
    }
    Ok((rows, false))
}
