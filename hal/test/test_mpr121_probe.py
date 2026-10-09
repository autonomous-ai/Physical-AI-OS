"""Read-only sensor diagnostics decoding and measurement summaries."""

import unittest
from unittest.mock import Mock

from hal.scripts.mpr121_probe import read_sample, read_settings, summarize


class TestMPR121Probe(unittest.TestCase):
    def test_read_decodes_faults_baseline_and_signed_delta_without_writes(self):
        data = bytearray(42)
        data[:4] = bytes((5, 0x80, 2, 0xc0))
        for i in range(12):
            data[4 + 2*i:6 + 2*i] = (700 + i).to_bytes(2, 'little')
            data[30+i] = 176
        bus = Mock()
        bus.read_regs.return_value = bytes(data)
        sample = read_sample(bus, 0x5a)
        self.assertEqual(sample['mask'], 5)
        self.assertTrue(sample['overcurrent'])
        self.assertEqual(sample['out_of_range_mask'], 2)
        self.assertTrue(sample['autoconfig_failed'])
        self.assertTrue(sample['autoreconfig_failed'])
        self.assertEqual(sample['delta'], list(range(4, -8, -1)))
        self.assertEqual(sample['baseline'], [704]*12)
        bus.read_regs.assert_called_once_with(0x5a, 0, 42)
        bus.write_reg.assert_not_called()

    def test_short_reads_fail_instead_of_reporting_zero_noise(self):
        bus = Mock()
        bus.read_regs.return_value = bytes(41)
        with self.assertRaises(OSError):
            read_sample(bus, 0x5a)

    def test_settings_keep_each_electrode_threshold(self):
        bus = Mock()
        bus.read_regs.side_effect = [bytes(range(24)), bytes((0x22, 0xd0, 0x30, 0x8f)), bytes(5), bytes(19)]
        settings = read_settings(bus, 0x5a)
        self.assertEqual(settings['touch_thresholds'], list(range(0, 24, 2)))
        self.assertEqual(settings['release_thresholds'], list(range(1, 24, 2)))
        self.assertEqual(settings['electrode_config'], 0x8f)
        bus.write_reg.assert_not_called()

    def test_summary_counts_transitions_and_faults_without_labelling_touches(self):
        samples = []
        for i, mask in enumerate((0, 7, 7, 0)):
            samples.append(dict(mask=mask, delta=[i-1]*12, overcurrent=False,
                                out_of_range_mask=int(i == 3), autoconfig_failed=False,
                                autoreconfig_failed=False))
        summary = summarize(samples)
        self.assertEqual(summary['mask_transitions'], 2)
        self.assertEqual(summary['fault_samples'], 1)
        self.assertEqual(summary['electrodes'][0], dict(electrode=0, delta_min=-1,
                         delta_p50=0, delta_p99=2, delta_max=2, active_samples=2))
        self.assertEqual(summary['electrodes'][3]['active_samples'], 0)
        self.assertEqual(summarize([]), {'samples': 0})
