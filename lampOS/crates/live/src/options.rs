//! Explicit local experiment settings. AEC is mandatory in both modes; these
//! options never change provider settings, gains, VAD, or output authority.
use serde::{Deserialize, Serialize};
use std::{io, path::PathBuf, str::FromStr};

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NoiseSuppression {
    #[default]
    On,
    Off,
}
impl NoiseSuppression {
    pub fn enabled(self) -> bool {
        self == Self::On
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::On => "on",
            Self::Off => "off",
        }
    }
    pub fn software_processing(self) -> SoftwareProcessing {
        SoftwareProcessing {
            aec: SoftwareAec::SonoraAec3,
            noise_suppression: self.enabled(),
        }
    }
}
impl FromStr for NoiseSuppression {
    type Err = io::Error;
    fn from_str(value: &str) -> io::Result<Self> {
        match value {
            "on" => Ok(Self::On),
            "off" => Ok(Self::Off),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "noise suppression must be exactly on or off; AEC remains enabled",
            )),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum SoftwareAec {
    SonoraAec3,
}

/// Immutable provenance for the actual capture pipeline. Missing provenance in
/// a generic recorder or historical file does not imply either mode.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct SoftwareProcessing {
    aec: SoftwareAec,
    noise_suppression: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct DirectedOptions {
    pub diagnostics: bool,
    pub noise_suppression: NoiseSuppression,
}
impl DirectedOptions {
    pub fn parse(arguments: &[String]) -> io::Result<Self> {
        let mut options = Self::default();
        let mut noise_seen = false;
        let mut arguments = arguments.iter();
        while let Some(argument) = arguments.next() {
            match argument.as_str() {
                "--diagnostics" if !options.diagnostics => options.diagnostics = true,
                "--noise-suppression" if !noise_seen => {
                    options.noise_suppression = required_value(&mut arguments)?.parse()?;
                    noise_seen = true;
                }
                _ => return Err(invalid_options()),
            }
        }
        Ok(options)
    }
}

/// Internal child CLI: only named options can identify mode or diagnostic path.
/// Parsing finishes before IPC binding, recorder creation or device opening.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct AudioWorkerOptions {
    pub diagnostic_directory: Option<PathBuf>,
    pub noise_suppression: NoiseSuppression,
}
impl AudioWorkerOptions {
    /// The exact argv tail used by the supervisor. Values remain individual
    /// arguments (including paths with spaces); no shell quoting is involved.
    pub fn child_arguments(&self, device: &str, is_capture: bool) -> io::Result<Vec<String>> {
        if device.is_empty() || (!is_capture && self.noise_suppression != NoiseSuppression::On) {
            return Err(invalid_options());
        }
        let mut arguments = vec![device.to_owned()];
        if is_capture {
            arguments.extend([
                "--noise-suppression".to_owned(),
                self.noise_suppression.as_str().to_owned(),
            ]);
        }
        if let Some(path) = &self.diagnostic_directory {
            if !path.is_absolute() {
                return Err(invalid_options());
            }
            arguments.extend([
                "--diagnostics".to_owned(),
                path.to_str().ok_or_else(invalid_options)?.to_owned(),
            ]);
        }
        Ok(arguments)
    }

    pub fn parse(arguments: &[String], is_capture: bool) -> io::Result<Self> {
        let mut options = Self::default();
        let mut noise_seen = false;
        let mut arguments = arguments.iter();
        while let Some(argument) = arguments.next() {
            match argument.as_str() {
                "--diagnostics" if options.diagnostic_directory.is_none() => {
                    let path = PathBuf::from(required_value(&mut arguments)?);
                    if !path.is_absolute() {
                        return Err(invalid_options());
                    }
                    options.diagnostic_directory = Some(path);
                }
                "--noise-suppression" if is_capture && !noise_seen => {
                    options.noise_suppression = required_value(&mut arguments)?.parse()?;
                    noise_seen = true;
                }
                _ => return Err(invalid_options()),
            }
        }
        Ok(options)
    }
}

fn required_value<'a>(arguments: &mut impl Iterator<Item = &'a String>) -> io::Result<&'a str> {
    arguments
        .next()
        .map(String::as_str)
        .filter(|value| !value.starts_with("--"))
        .ok_or_else(invalid_options)
}
fn invalid_options() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "missing, duplicate or unknown audio experiment option",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }
    #[test]
    fn default_is_the_existing_aec_plus_noise_suppression_pipeline() {
        let options = DirectedOptions::parse(&[]).unwrap();
        assert_eq!(options, DirectedOptions::default());
        assert!(options.noise_suppression.enabled());
        assert!(!options.diagnostics);
        assert_eq!(
            serde_json::to_value(options.noise_suppression.software_processing()).unwrap(),
            serde_json::json!({"aec":"sonora_aec3","noise_suppression":true})
        );
    }
    #[test]
    fn both_modes_are_orthogonal_to_diagnostics_in_either_order() {
        for mode in [NoiseSuppression::On, NoiseSuppression::Off] {
            for flags in [
                vec!["--noise-suppression", mode.as_str(), "--diagnostics"],
                vec!["--diagnostics", "--noise-suppression", mode.as_str()],
            ] {
                assert_eq!(
                    DirectedOptions::parse(&args(&flags)).unwrap(),
                    DirectedOptions {
                        diagnostics: true,
                        noise_suppression: mode
                    }
                );
            }
            let only =
                DirectedOptions::parse(&args(&["--noise-suppression", mode.as_str()])).unwrap();
            assert!(!only.diagnostics);
            assert_eq!(only.noise_suppression, mode);
            assert_eq!(
                serde_json::to_value(mode.software_processing()).unwrap(),
                serde_json::json!({"aec":"sonora_aec3","noise_suppression":mode.enabled()})
            );
        }
    }
    #[test]
    fn invalid_or_ambiguous_flags_fail_before_any_startup_work() {
        for flags in [
            vec!["--noise-suppression"],
            vec!["--noise-suppression", "false"],
            vec!["--noise-suppression", "OFF"],
            vec!["--noise-suppression", "--diagnostics"],
            vec!["--noise-suppression", "off", "--noise-suppression", "on"],
            vec!["--diagnostics", "--diagnostics"],
            vec!["--aec", "off"],
            vec!["off"],
            vec!["/tmp/audio"],
            vec!["--diagnostics", "/tmp/audio"],
        ] {
            assert!(DirectedOptions::parse(&args(&flags)).is_err(), "{flags:?}");
        }
    }
    #[test]
    fn child_options_are_explicit_and_render_cannot_claim_capture_processing() {
        for mode in [NoiseSuppression::On, NoiseSuppression::Off] {
            let parsed = AudioWorkerOptions::parse(
                &args(&[
                    "--noise-suppression",
                    mode.as_str(),
                    "--diagnostics",
                    "/tmp/new-capture",
                ]),
                true,
            )
            .unwrap();
            assert_eq!(parsed.noise_suppression, mode);
            assert_eq!(
                parsed.diagnostic_directory,
                Some(PathBuf::from("/tmp/new-capture"))
            );
        }
        assert_eq!(
            AudioWorkerOptions::parse(&[], true)
                .unwrap()
                .noise_suppression,
            NoiseSuppression::On
        );
        assert!(AudioWorkerOptions::parse(&args(&["--noise-suppression", "off"]), false).is_err());
        for flags in [
            vec!["off"],
            vec!["/tmp/new-capture"],
            vec!["--diagnostics"],
            vec!["--diagnostics", "relative"],
            vec!["--diagnostics", "--noise-suppression", "off"],
            vec!["--diagnostics", "/tmp/a", "--diagnostics", "/tmp/b"],
        ] {
            assert!(
                AudioWorkerOptions::parse(&args(&flags), true).is_err(),
                "{flags:?}"
            );
        }
    }

    #[test]
    fn supervisor_argv_preserves_requested_capture_mode_and_separate_render_path() {
        for mode in [NoiseSuppression::On, NoiseSuppression::Off] {
            for directory in [None, Some(PathBuf::from("/tmp/private run/capture-audio"))] {
                let requested = AudioWorkerOptions {
                    diagnostic_directory: directory,
                    noise_suppression: mode,
                };
                let arguments = requested
                    .child_arguments("plug:device_micro2", true)
                    .unwrap();
                assert_eq!(arguments[0], "plug:device_micro2");
                let actual = AudioWorkerOptions::parse(&arguments[1..], true).unwrap();
                assert_eq!(actual, requested);
            }
        }
        let render = AudioWorkerOptions {
            diagnostic_directory: Some(PathBuf::from("/tmp/private run/render-audio")),
            ..AudioWorkerOptions::default()
        };
        let arguments = render
            .child_arguments("plug:device_speaker", false)
            .unwrap();
        assert_eq!(
            arguments,
            args(&[
                "plug:device_speaker",
                "--diagnostics",
                "/tmp/private run/render-audio"
            ])
        );
        assert_eq!(
            AudioWorkerOptions::parse(&arguments[1..], false).unwrap(),
            render
        );
    }
}
