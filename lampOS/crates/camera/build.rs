use sha2::{Digest, Sha256};
use std::{env, fs, path::PathBuf};

const HEADER_ENV: &str = "V4L2R_VIDEODEV2_H_PATH";
const BUNDLE: &str = "vendor/linux-uapi-v6.12-adc218676eef/include";
const HEADERS: &[(&str, u64, &str)] = &[
    (
        "linux/videodev2.h",
        103727,
        "81d95175596837bfd89a0645e2970dbddcb31f328cbdca4ebe88b3daecd19504",
    ),
    (
        "linux/v4l2-common.h",
        2056,
        "db7d65a8a88a520f43abc3821109a60e600ced91756027362059a16945802427",
    ),
    (
        "linux/v4l2-controls.h",
        149407,
        "d8b27bd5d3b3b03626dc2b87a522bb874218d5d0352e20931ee89e477c60f96b",
    ),
    (
        "videodev2.h",
        29,
        "59b2db1e75ff6926fdec6a7d3c0eb25f49facde3a13c2925d15f89e13fad5a3e",
    ),
];

fn main() {
    println!("cargo::rerun-if-env-changed={HEADER_ENV}");
    if let Err(error) = verify_bundle() {
        panic!("camera UAPI build input check failed: {error}");
    }
}

fn verify_bundle() -> Result<(), String> {
    let crate_dir =
        PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").ok_or("missing crate directory")?)
            .canonicalize()
            .map_err(|error| format!("resolve crate directory: {error}"))?;
    let workspace = crate_dir
        .parent()
        .and_then(|path| path.parent())
        .ok_or("camera crate is not inside the standalone workspace")?;
    let expected_root = workspace.join(BUNDLE);
    let canonical_root = expected_root
        .canonicalize()
        .map_err(|error| format!("missing bundled camera headers: {error}"))?;
    if canonical_root != expected_root {
        return Err(
            "bundled camera headers must be inside the workspace without symlink indirection"
                .into(),
        );
    }
    let selected = env::var(HEADER_ENV).map_err(|_| {
        format!("{HEADER_ENV} is missing or invalid; build from the lampOS workspace root so .cargo/config.toml is loaded")
    })?;
    let selected_root = PathBuf::from(selected)
        .canonicalize()
        .map_err(|error| format!("resolve selected camera headers: {error}"))?;
    if selected_root != expected_root {
        return Err(format!(
            "{HEADER_ENV} must select the bundled camera headers"
        ));
    }

    for &(relative, length, digest) in HEADERS {
        let path = expected_root.join(relative);
        println!("cargo::rerun-if-changed={}", path.display());
        let metadata = fs::symlink_metadata(&path)
            .map_err(|error| format!("read bundled header metadata {relative}: {error}"))?;
        if !metadata.is_file() || metadata.len() != length {
            return Err(format!(
                "bundled header {relative} is not a regular file of the pinned length"
            ));
        }
        if path
            .canonicalize()
            .map_err(|error| format!("resolve bundled header {relative}: {error}"))?
            != path
        {
            return Err(format!(
                "bundled header {relative} must not traverse symlinks"
            ));
        }
        let bytes =
            fs::read(&path).map_err(|error| format!("read bundled header {relative}: {error}"))?;
        if format!("{:x}", Sha256::digest(&bytes)) != digest {
            return Err(format!(
                "bundled header {relative} does not match its pinned SHA256"
            ));
        }
    }
    Ok(())
}
