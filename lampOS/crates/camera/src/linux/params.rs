use super::map_ioctl;
use crate::backend::{Check, Fault, Stage, checked};
use crate::{CaptureConfig, FrameInterval, NegotiatedMode, PixelFormat, PortError};
use std::os::fd::OwnedFd;
use v4l2r::{Format, QueueType, bindings, ioctl};

const QUEUE: QueueType = QueueType::VideoCapture;
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CapabilityReport {
    pub driver: String,
    pub card: String,
    pub bus_info: String,
    pub version: u32,
    pub capabilities: u32,
    pub device_capabilities: u32,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IntervalStatus {
    Unsupported,
    Reported {
        can_set_interval: bool,
        actual: Option<FrameInterval>,
    },
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ControlState {
    Unsupported,
    Supported {
        name: String,
        kind: u32,
        flags: u32,
        minimum: i32,
        maximum: i32,
        step: i32,
        default: i32,
        reading: ControlReading,
    },
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ControlReading {
    Value(i32),
    Disabled,
    UnsupportedType,
    Unavailable(i32),
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ControlObservation {
    pub id: u32,
    pub state: ControlState,
}
const CONTROLS: [u32; 5] = [
    bindings::V4L2_CID_EXPOSURE_AUTO,
    bindings::V4L2_CID_EXPOSURE_ABSOLUTE,
    bindings::V4L2_CID_GAIN,
    bindings::V4L2_CID_BRIGHTNESS,
    bindings::V4L2_CID_AUTO_WHITE_BALANCE,
];

pub(super) fn fixed_string(bytes: &[u8]) -> Result<String, PortError> {
    let end = bytes
        .iter()
        .position(|b| *b == 0)
        .ok_or(PortError::InvalidData)?;
    let value = std::str::from_utf8(&bytes[..end]).map_err(|_| PortError::InvalidData)?;
    if !value.bytes().all(|b| b.is_ascii_graphic() || b == b' ') {
        return Err(PortError::InvalidData);
    }
    Ok(value.to_owned())
}

pub(super) fn capabilities(
    fd: &OwnedFd,
    check: &mut impl Check,
) -> Result<CapabilityReport, Fault> {
    let raw: bindings::v4l2_capability = checked(check, Stage::Capabilities, || {
        ioctl::querycap(fd).map_err(map_ioctl)
    })?;
    let caps = if raw.capabilities & bindings::V4L2_CAP_DEVICE_CAPS != 0 {
        raw.device_caps
    } else {
        raw.capabilities
    };
    let required = bindings::V4L2_CAP_VIDEO_CAPTURE | bindings::V4L2_CAP_STREAMING;
    if caps & required != required
        || caps & (bindings::V4L2_CAP_VIDEO_M2M | bindings::V4L2_CAP_VIDEO_M2M_MPLANE) != 0
    {
        return Err(Fault::new(Stage::Capabilities, PortError::Unsupported));
    }
    let report = CapabilityReport {
        driver: fixed_string(&raw.driver).map_err(|e| Fault::new(Stage::Capabilities, e))?,
        card: fixed_string(&raw.card).map_err(|e| Fault::new(Stage::Capabilities, e))?,
        bus_info: fixed_string(&raw.bus_info).map_err(|e| Fault::new(Stage::Capabilities, e))?,
        version: raw.version,
        capabilities: raw.capabilities,
        device_capabilities: caps,
    };
    if report.driver.is_empty() || report.card.is_empty() || report.bus_info.is_empty() {
        return Err(Fault::invalid(Stage::Capabilities));
    }
    Ok(report)
}
fn format(
    raw: bindings::v4l2_format,
    config: CaptureConfig,
    stage: Stage,
) -> Result<Format, Fault> {
    if raw.type_ != QUEUE as u32 {
        return Err(Fault::invalid(stage));
    }
    let actual = Format::try_from(raw).map_err(|_| Fault::invalid(stage))?;
    if actual.width != config.width
        || actual.height != config.height
        || <[u8; 4]>::from(actual.pixelformat) != *b"MJPG"
        || actual.plane_fmt.len() != 1
        || actual.plane_fmt[0].sizeimage < 4
        || actual.plane_fmt[0].sizeimage as usize > config.max_frame_bytes
    {
        return Err(Fault::invalid(stage));
    }
    Ok(actual)
}

#[derive(Clone, Copy)]
struct CaptureParameters {
    capability: u32,
    numerator: u32,
    denominator: u32,
}
/// The only Lamp-owned union read. The ioctl returned an initialized owned
/// value, and `type_` is checked before interpreting its capture member. The
/// member consists only of integer fields (all bit patterns are valid); no
/// pointer or reference is exposed. Other queue types are rejected.
#[allow(unsafe_code)]
fn capture_parameters(raw: bindings::v4l2_streamparm) -> Result<CaptureParameters, PortError> {
    if raw.type_ != QUEUE as u32 {
        return Err(PortError::InvalidData);
    }
    // SAFETY: validated capture discriminator; copy the plain integer member.
    let capture = unsafe { raw.parm.capture };
    Ok(CaptureParameters {
        capability: capture.capability,
        numerator: capture.timeperframe.numerator,
        denominator: capture.timeperframe.denominator,
    })
}
fn get_interval(fd: &OwnedFd, check: &mut impl Check) -> Result<IntervalStatus, Fault> {
    let raw = match checked(check, Stage::GetInterval, || {
        ioctl::g_parm::<bindings::v4l2_streamparm>(fd, QUEUE).map_err(map_ioctl)
    }) {
        Ok(raw) => raw,
        Err(Fault {
            error: PortError::Io(22 | 25),
            ..
        }) => return Ok(IntervalStatus::Unsupported),
        Err(error) => return Err(error),
    };
    let params = capture_parameters(raw).map_err(|e| Fault::new(Stage::GetInterval, e))?;
    let actual = if params.numerator == 0 || params.denominator == 0 {
        None
    } else {
        Some(
            FrameInterval::new(params.numerator, params.denominator)
                .map_err(|_| Fault::invalid(Stage::GetInterval))?,
        )
    };
    Ok(IntervalStatus::Reported {
        can_set_interval: params.capability & bindings::V4L2_CAP_TIMEPERFRAME != 0,
        actual,
    })
}
pub(super) fn negotiate(
    fd: &mut OwnedFd,
    config: CaptureConfig,
    check: &mut impl Check,
) -> Result<(NegotiatedMode, IntervalStatus), Fault> {
    let requested = Format::from((b"MJPG", (config.width as usize, config.height as usize)));
    let raw: bindings::v4l2_format = checked(check, Stage::SetFormat, || {
        ioctl::s_fmt(fd, (QUEUE, &requested)).map_err(map_ioctl)
    })?;
    format(raw, config, Stage::SetFormat)?;
    let raw: bindings::v4l2_format = checked(check, Stage::GetFormat, || {
        ioctl::g_fmt(fd, QUEUE).map_err(map_ioctl)
    })?;
    let actual = format(raw, config, Stage::GetFormat)?;
    let mut interval = get_interval(fd, check)?;
    if let Some(requested) = config.interval {
        if !matches!(
            interval,
            IntervalStatus::Reported {
                can_set_interval: true,
                ..
            }
        ) {
            return Err(Fault::new(Stage::SetInterval, PortError::Unsupported));
        }
        let mut raw = bindings::v4l2_streamparm {
            type_: QUEUE as u32,
            ..Default::default()
        };
        // Writing a union member initializes it and requires no unsafe access.
        raw.parm.capture = bindings::v4l2_captureparm {
            timeperframe: bindings::v4l2_fract {
                numerator: requested.numerator(),
                denominator: requested.denominator(),
            },
            ..Default::default()
        };
        let returned: bindings::v4l2_streamparm = checked(check, Stage::SetInterval, || {
            ioctl::s_parm(fd, raw).map_err(map_ioctl)
        })?;
        capture_parameters(returned).map_err(|e| Fault::new(Stage::SetInterval, e))?;
        interval = get_interval(fd, check)?;
    }
    let mode = NegotiatedMode {
        source: config.source,
        format: PixelFormat::MJPG,
        width: actual.width,
        height: actual.height,
        size_image: actual.plane_fmt[0].sizeimage as usize,
        buffers: config.buffers,
        interval: match interval {
            IntervalStatus::Unsupported => None,
            IntervalStatus::Reported { actual, .. } => actual,
        },
    };
    mode.validate(config)
        .map_err(|_| Fault::invalid(Stage::GetInterval))?;
    Ok((mode, interval))
}

pub(super) fn controls(
    fd: &OwnedFd,
    check: &mut impl Check,
) -> Result<[ControlObservation; 5], Fault> {
    let mut result = std::array::from_fn(|index| ControlObservation {
        id: CONTROLS[index],
        state: ControlState::Unsupported,
    });
    for (slot, id) in result.iter_mut().zip(CONTROLS) {
        let query_id = ioctl::CtrlId::new(id).map_err(|_| Fault::invalid(Stage::Controls))?;
        let raw: bindings::v4l2_queryctrl = match checked(check, Stage::Controls, || {
            ioctl::queryctrl(fd, query_id, ioctl::QueryCtrlFlags::empty()).map_err(map_ioctl)
        }) {
            Ok(raw) => raw,
            Err(Fault {
                error: PortError::Io(22 | 25),
                ..
            }) => continue,
            Err(error) => return Err(error),
        };
        if raw.id != id
            || raw.minimum > raw.maximum
            || raw.step < 0
            || !(raw.minimum..=raw.maximum).contains(&raw.default_value)
        {
            return Err(Fault::invalid(Stage::Controls));
        }
        let reading = if raw.flags & bindings::V4L2_CTRL_FLAG_DISABLED != 0 {
            ControlReading::Disabled
        } else if !(1..=3).contains(&raw.type_) {
            ControlReading::UnsupportedType
        } else {
            match checked(check, Stage::Controls, || {
                ioctl::g_ctrl(fd, id).map_err(map_ioctl)
            }) {
                Ok(value) if (raw.minimum..=raw.maximum).contains(&value) => {
                    ControlReading::Value(value)
                }
                Ok(_) => return Err(Fault::invalid(Stage::Controls)),
                Err(Fault {
                    error: PortError::Io(errno @ (13 | 22 | 25)),
                    ..
                }) => ControlReading::Unavailable(errno),
                Err(error) => return Err(error),
            }
        };
        slot.state = ControlState::Supported {
            name: fixed_string(&raw.name).map_err(|e| Fault::new(Stage::Controls, e))?,
            kind: raw.type_,
            flags: raw.flags,
            minimum: raw.minimum,
            maximum: raw.maximum,
            step: raw.step,
            default: raw.default_value,
            reading,
        };
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn capture_union_requires_exact_discriminator_and_copies_valid_numbers() {
        let mut raw = bindings::v4l2_streamparm {
            type_: QUEUE as u32,
            ..Default::default()
        };
        raw.parm.capture = bindings::v4l2_captureparm {
            capability: bindings::V4L2_CAP_TIMEPERFRAME,
            timeperframe: bindings::v4l2_fract {
                numerator: 1,
                denominator: 30,
            },
            ..Default::default()
        };
        let actual = capture_parameters(raw).unwrap();
        assert_eq!((actual.numerator, actual.denominator), (1, 30));
        for kind in [
            0,
            QueueType::VideoOutput as u32,
            QueueType::VideoCaptureMplane as u32,
            u32::MAX,
        ] {
            raw.type_ = kind;
            assert!(capture_parameters(raw).is_err());
        }
    }
    #[test]
    fn format_reply_cannot_change_queue_codec_dimensions_or_exceed_image_bound() {
        let config = CaptureConfig {
            source: crate::SourceId::new("camera").unwrap(),
            width: 1280,
            height: 720,
            interval: None,
            max_frame_bytes: 4096,
            buffers: 2,
        };
        let make = |queue, width, codec, size| {
            let value = Format {
                width,
                height: 720,
                pixelformat: codec,
                plane_fmt: vec![v4l2r::PlaneLayout {
                    sizeimage: size,
                    bytesperline: 0,
                }],
            };
            bindings::v4l2_format::try_from((queue, &value)).unwrap()
        };
        assert!(
            format(
                make(QUEUE, 1280, b"MJPG".into(), 4096),
                config,
                Stage::GetFormat
            )
            .is_ok()
        );
        for raw in [
            make(QueueType::VideoOutput, 1280, b"MJPG".into(), 4096),
            make(QUEUE, 640, b"MJPG".into(), 4096),
            make(QUEUE, 1280, b"YUYV".into(), 4096),
            make(QUEUE, 1280, b"MJPG".into(), 0),
            make(QUEUE, 1280, b"MJPG".into(), 4097),
        ] {
            assert!(format(raw, config, Stage::GetFormat).is_err());
        }
    }
    #[test]
    fn bounded_kernel_strings_do_not_hide_invalid_utf8_or_missing_terminator() {
        assert_eq!(fixed_string(b"UVC camera\0tail").unwrap(), "UVC camera");
        assert!(fixed_string(b"no terminator").is_err());
        assert!(fixed_string(&[255, 0]).is_err());
        assert!(fixed_string(b"line\n\0").is_err());
    }
}
