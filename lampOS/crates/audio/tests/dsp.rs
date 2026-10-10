use lamp_audio::{
    CAPTURE_SAMPLES, EchoProcessor, RENDER_SAMPLES, float_to_pcm,
    qualification::{benchmark, synthetic_echo_block},
};

#[test]
fn pcm_extremes_saturate_without_sign_wrap() {
    assert_eq!(float_to_pcm(1.0), i16::MAX);
    assert_eq!(float_to_pcm(-1.0), i16::MIN);
    assert_eq!(float_to_pcm(2.0), i16::MAX);
    assert_eq!(float_to_pcm(-2.0), i16::MIN);
}

#[test]
fn silence_stays_silent_at_native_provider_and_capture_rates() {
    let mut processor = EchoProcessor::new(true);
    for _ in 0..100 {
        processor.render(&[0; RENDER_SAMPLES]).unwrap();
        assert_eq!(
            processor.capture(&[0; CAPTURE_SAMPLES], 0).unwrap(),
            [0; CAPTURE_SAMPLES]
        );
    }
}

#[test]
fn impossible_delay_is_rejected_without_poisoning_next_frame() {
    let mut processor = EchoProcessor::new(true);
    assert!(processor.capture(&[0; CAPTURE_SAMPLES], 501).is_err());
    processor.render(&[0; RENDER_SAMPLES]).unwrap();
    assert_eq!(
        processor.capture(&[0; CAPTURE_SAMPLES], 0).unwrap(),
        [0; CAPTURE_SAMPLES]
    );
}

#[test]
fn reset_forgets_previous_reference_and_matches_a_fresh_processor() {
    let mut processor = EchoProcessor::new(true);
    for i in 0..100 {
        let (render, capture) = synthetic_echo_block(i);
        processor.render(&render).unwrap();
        processor.capture(&capture, 70).unwrap();
    }
    processor.reset();
    let mut fresh = EchoProcessor::new(true);
    for i in 100..150 {
        let (render, capture) = synthetic_echo_block(i);
        processor.render(&render).unwrap();
        fresh.render(&render).unwrap();
        assert_eq!(
            processor.capture(&capture, 70).unwrap(),
            fresh.capture(&capture, 70).unwrap()
        );
    }
}

#[test]
fn echo_fixture_is_attenuated_after_warmup_without_erasing_all_near_end() {
    let report = benchmark(500).unwrap();
    assert!(report.synthetic_echo_reduction_db >= 6.0, "{report:?}");
    // This is a signal-integrity floor, not a claim of natural double-talk quality.
    assert!(
        report.near_end_with_silent_render_output_input_rms_ratio > 0.02,
        "{report:?}"
    );
}

#[test]
fn benchmark_resource_bounds_are_explicit() {
    assert!(benchmark(499).is_err());
    assert!(benchmark(60001).is_err());
}

#[test]
fn cold_report_starts_at_first_capture_and_keeps_reset_near_end_separate() {
    let report = benchmark(500).unwrap();
    let mut echo = EchoProcessor::new(true);
    let mut near = EchoProcessor::new(true);
    let mut input_energy = 0.0;
    let mut echo_energy = 0.0;
    let mut near_energy = 0.0;
    for block in 0..25 {
        let (render, capture) = synthetic_echo_block(block);
        echo.render(&render).unwrap();
        near.render(&[0; RENDER_SAMPLES]).unwrap();
        let echo_output = echo.capture(&capture, 70).unwrap();
        let near_output = near.capture(&capture, 0).unwrap();
        input_energy += capture
            .iter()
            .map(|&sample| f64::from(sample).powi(2))
            .sum::<f64>();
        echo_energy += echo_output
            .iter()
            .map(|&sample| f64::from(sample).powi(2))
            .sum::<f64>();
        near_energy += near_output
            .iter()
            .map(|&sample| f64::from(sample).powi(2))
            .sum::<f64>();
    }
    let first_echo = &report.cold_start_echo[0];
    let first_near = &report.cold_start_near_end_with_silent_render[0];
    assert_eq!(first_echo.frames, 25 * CAPTURE_SAMPLES);
    assert_eq!(first_echo.input_rms_pcm, (input_energy / 4000.0).sqrt());
    assert_eq!(first_echo.output_rms_pcm, (echo_energy / 4000.0).sqrt());
    assert_eq!(first_near.output_rms_pcm, (near_energy / 4000.0).sqrt());
    assert_ne!(first_echo.output_rms_pcm, first_near.output_rms_pcm);
}
