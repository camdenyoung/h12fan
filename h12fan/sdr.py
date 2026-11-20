"""Read linear fan conversion factors from ipmitool's binary SDR dump.

Full sensor record layout: IPMI 2.0 table 43-1. Only unsigned, linear fan
sensors are supported; unknown layouts retain strict read-back comparison.
"""
import math
from dataclasses import dataclass


def signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


@dataclass(frozen=True)
class FanScale:
    m: int
    b: int
    b_exp: int
    r_exp: int

    def quantize(self, rpm):
        offset = self.b * 10 ** self.b_exp
        raw = (rpm / 10 ** self.r_exp - offset) / self.m
        raw = math.floor(raw + 0.5)
        if not 0 <= raw <= 255:
            raise ValueError('Requested threshold is outside the sensor range')
        return round((self.m * raw + offset) * 10 ** self.r_exp, 3)


def fan_scales(data):
    scales = {}
    cursor = 0
    while cursor < len(data):
        if len(data) - cursor < 5:
            raise ValueError('Truncated SDR header')
        kind, size = data[cursor + 3], data[cursor + 4]
        cursor += 5
        body = data[cursor:cursor + size]
        cursor += size
        if len(body) != size:
            raise ValueError('Truncated SDR record')
        if kind != 1 or size < 43:
            continue
        # Fan sensor type, threshold events, unsigned values and linear conversion.
        if body[7] != 4 or body[8] != 1 or body[15] >> 6 != 0 or body[18] & 0x7f:
            continue
        code = body[42]
        length = code & 0x1f
        if code >> 6 != 3 or not 0 < length <= 16 or size < 43 + length:
            continue
        try:
            name = body[43:43 + length].decode('ascii').strip()
        except UnicodeDecodeError:
            continue
        m = signed(body[19] | ((body[20] & 0xc0) << 2), 10)
        b = signed(body[21] | ((body[22] & 0xc0) << 2), 10)
        if m > 0 and name:
            scales[name] = FanScale(m, b, signed(body[24] & 0xf, 4), signed(body[24] >> 4, 4))
    return scales


def masked_lower(values, actual, scale=None):
    expected = [None if available is None else scale.quantize(value) if scale else value
                for value, available in zip(values, actual)]
    present = [v for v in expected if v is not None]
    if any(a >= b for a, b in zip(present, present[1:])):
        raise ValueError('Supported thresholds must remain distinct and increasing after BMC rounding')
    return expected
