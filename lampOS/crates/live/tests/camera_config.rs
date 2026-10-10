use lamp_live::{
    camera_config::CameraConfig, camera_inspect::MetadataReport, process::SessionDirectory,
};
use serde_json::json;
use std::{
    fs,
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt, symlink},
};
fn value() -> serde_json::Value {
    json!({"device":"/dev/explicit-camera","lock_directory":"/run/private-camera-locks","source":"desk-camera",
    "expected_usb":{"vendor":4660,"product":22136,"topology":"1-2.3","serial":null,"interface_number":0,"capture_index":0},
    "width":640,"height":480,"interval":null,"max_frame_bytes":4096,"buffers":2})
}
struct ConfigFile {
    dir: SessionDirectory,
    path: std::path::PathBuf,
}
impl ConfigFile {
    fn new() -> Self {
        let dir = SessionDirectory::create().unwrap();
        let path = dir.path.join("camera.json");
        fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .unwrap()
            .write_all(&serde_json::to_vec(&value()).unwrap())
            .unwrap();
        Self { dir, path }
    }
    fn write(&self, value: serde_json::Value) {
        fs::write(&self.path, serde_json::to_vec(&value).unwrap()).unwrap();
    }
}
impl Drop for ConfigFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}
#[test]
fn exact_configuration_digest_is_checked_again_before_child_start() {
    let file = ConfigFile::new();
    let (_, digest) = CameraConfig::load(&file.path, None).unwrap();
    assert_eq!(digest.len(), 64);
    CameraConfig::load(&file.path, Some(&digest)).unwrap();
    let mut v = value();
    v["width"] = json!(320);
    file.write(v);
    assert!(CameraConfig::load(&file.path, Some(&digest)).is_err());
}
#[test]
fn identity_path_format_and_size_have_no_defaults_or_fallbacks() {
    let file = ConfigFile::new();
    for (field, replacement) in [
        ("device", json!("relative-video")),
        ("source", json!("")),
        ("buffers", json!(1)),
        ("width", json!(1920)),
        ("max_frame_bytes", json!(2_097_153)),
        ("interval", json!({"numerator":0,"denominator":30})),
    ] {
        let mut v = value();
        v[field] = replacement;
        file.write(v);
        assert!(CameraConfig::load(&file.path, None).is_err(), "{field}");
    }
    let mut v = value();
    v.as_object_mut().unwrap().remove("device");
    file.write(v);
    assert!(CameraConfig::load(&file.path, None).is_err());
    for key in ["allowed", "camera_allowed", "ignore_privacy"] {
        let mut v = value();
        v[key] = json!(true);
        file.write(v);
        assert!(CameraConfig::load(&file.path, None).is_err());
    }
    let mut v = value();
    v["expected_usb"]["vendor"] = json!(0);
    file.write(v);
    assert!(CameraConfig::load(&file.path, None).is_err());
}
#[test]
fn private_regular_bounded_single_link_file_required() {
    let file = ConfigFile::new();
    let link = file.dir.path.join("alias.json");
    symlink(&file.path, &link).unwrap();
    assert!(CameraConfig::load(&link, None).is_err());
    fs::remove_file(&link).unwrap();
    fs::hard_link(&file.path, &link).unwrap();
    assert!(CameraConfig::load(&file.path, None).is_err());
    fs::remove_file(&link).unwrap();
    fs::set_permissions(&file.path, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(CameraConfig::load(&file.path, None).is_err());
    fs::set_permissions(&file.path, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&file.path, vec![b' '; 8193]).unwrap();
    assert!(CameraConfig::load(&file.path, None).is_err());
}
#[test]
fn finite_duration_is_validated_before_any_worker_or_device() {
    let config: CameraConfig = serde_json::from_value(value()).unwrap();
    for duration in [0, 61, u64::MAX] {
        assert!(MetadataReport::new(config.clone(), "a".repeat(64), duration, 0).is_err());
    }
    assert!(MetadataReport::new(config, "a".repeat(64), 60, 0).is_ok());
}
