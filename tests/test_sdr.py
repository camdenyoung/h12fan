import unittest
from h12fan.sdr import FanScale, fan_scales, masked_lower


def fan_record(name='FAN1', m=140, b=0, r_exp=0, b_exp=0):
    body = bytearray(43 + len(name))
    body[0] = 0x20
    body[7], body[8], body[16] = 4, 1, 18
    body[19], body[20] = m & 0xff, ((m & 0x300) >> 2)
    body[21], body[22] = b & 0xff, ((b & 0x300) >> 2)
    body[24] = ((r_exp & 0xf) << 4) | (b_exp & 0xf)
    body[42] = 0xc0 | len(name)
    body[43:] = name.encode('ascii')
    return bytes([1, 0, 0x51, 1, len(body)]) + body


class Tests(unittest.TestCase):
    def test_h12_rounding_uses_record_factors_and_skips_unreported_lnc(self):
        scale = fan_scales(fan_record())['FAN1']
        self.assertEqual(masked_lower([400, 800, 1000], [420, 840, None], scale), [420, 840, None])
        self.assertEqual(scale.quantize(1000), 980)

    def test_round_half_up_instead_of_python_bankers_rounding(self):
        self.assertEqual(FanScale(140, 0, 0, 0).quantize(350), 420)

    def test_signed_offset_and_exponents(self):
        scale = fan_scales(fan_record(m=200, b=-10, r_exp=-1, b_exp=1))['FAN1']
        self.assertEqual(scale, FanScale(200, -10, 1, -1))
        self.assertEqual(scale.quantize(400), 410)

    def test_nonlinear_or_signed_records_not_guessed(self):
        for index, value in [(18, 1), (15, 0x40), (7, 1)]:
            data = bytearray(fan_record())
            data[5 + index] = value
            self.assertEqual(fan_scales(data), {})

    def test_truncated_records_rejected(self):
        for data in [b'123', fan_record()[:-1]]:
            with self.assertRaises(ValueError):
                fan_scales(data)

    def test_raw_range_overflow_rejected_instead_of_wrapping(self):
        with self.assertRaises(ValueError):
            FanScale(140, 0, 0, 0).quantize(50000)

    def test_collapsed_rounded_thresholds_rejected(self):
        with self.assertRaises(ValueError):
            masked_lower([400, 450, 1000], [420, 840, None], FanScale(140, 0, 0, 0))
