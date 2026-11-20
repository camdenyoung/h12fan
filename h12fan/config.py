import copy
import json
import math
import os
import tempfile
from pathlib import Path


def defaults():
    return {
        'enabled': False, 'poll_seconds': 5,
        'threshold_check_seconds': 60, 'startup_seconds': 20, 'hysteresis': 3,
        'zones': [
            {'id': 0, 'name': 'Zone 1 · FAN1–FAN7', 'sensors': ['CPU Temp'], 'critical': 85,
             'points': [{'temp': 0, 'duty': 35}, {'temp': 40, 'duty': 45},
                        {'temp': 55, 'duty': 65}, {'temp': 70, 'duty': 100}]},
            {'id': 1, 'name': 'Zone 2 · FANA / FANB', 'sensors': ['System Temp'], 'critical': 70,
             'points': [{'temp': 0, 'duty': 35}, {'temp': 35, 'duty': 45},
                        {'temp': 45, 'duty': 65}, {'temp': 60, 'duty': 100}]},
        ],
        'fans': {name: {'desired': [400, 800, 1000], 'factory': None, 'restore': False}
                 for name in ['FAN1', 'FAN2', 'FAN3', 'FAN4', 'FAN5', 'FAN6', 'FAN7', 'FANA', 'FANB']},
    }


def number(value, low, high, label, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    if not low <= value <= high or (integer and value != int(value)):
        raise ValueError(f'{label} must be {low}–{high}' + (' (integer)' if integer else ''))


def triplet(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError('Thresholds require three RPM values: LNR, LCR, LNC')
    for n in value:
        number(n, 0, 50000, 'RPM', True)
    if not value[0] < value[1] < value[2]:
        raise ValueError('Thresholds must increase: LNR < LCR < LNC')


def validate(c):
    # Older releases stored an acknowledgement flag; accept and discard it.
    if isinstance(c, dict):
        c = copy.deepcopy(c)
        c.pop('hardware_confirmed', None)
    if not isinstance(c, dict) or set(c) != set(defaults()):
        raise ValueError('Incomplete or unknown configuration fields')
    if type(c['enabled']) is not bool:
        raise ValueError('enabled must be true or false')
    for key, lo, hi in [('poll_seconds', 2, 60), ('threshold_check_seconds', 30, 3600),
                        ('startup_seconds', 0, 300), ('hysteresis', 0, 15)]:
        number(c[key], lo, hi, key)
    if not isinstance(c['zones'], list) or len(c['zones']) != 2:
        raise ValueError('Both hardware zones (0 and 1) must be configured')
    for i, z in enumerate(c['zones']):
        if set(z) != {'id', 'name', 'sensors', 'critical', 'points'} or type(z['id']) is not int or z['id'] != i:
            raise ValueError('Zones must be ordered 0, 1')
        if not isinstance(z['name'], str) or not 1 <= len(z['name']) <= 64:
            raise ValueError('Zone name required (maximum 64 characters)')
        if not isinstance(z['sensors'], list) or not 1 <= len(z['sensors']) <= 32:
            raise ValueError('Select at least one temperature sensor per zone')
        if any(not isinstance(s, str) or not 1 <= len(s) <= 64 for s in z['sensors']):
            raise ValueError('Invalid sensor name')
        number(z['critical'], 20, 110, 'Critical temperature')
        if not isinstance(z['points'], list) or not 1 <= len(z['points']) <= 16:
            raise ValueError('Use 1–16 temperature steps')
        previous_temp, previous_duty = -41, 0
        for p in z['points']:
            if set(p) != {'temp', 'duty'}:
                raise ValueError('Each step needs temperature and duty')
            number(p['temp'], -40, 110, 'Temperature')
            number(p['duty'], 10, 100, 'Duty', True)
            if p['temp'] <= previous_temp or p['duty'] < previous_duty:
                raise ValueError('Temperatures must increase; duty must not decrease')
            previous_temp, previous_duty = p['temp'], p['duty']
    if not isinstance(c['fans'], dict) or len(c['fans']) > 32:
        raise ValueError('Invalid fan configuration')
    for name, fan in c['fans'].items():
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or name.startswith('-'):
            raise ValueError('Invalid fan sensor name')
        if set(fan) != {'desired', 'factory', 'restore'} or type(fan['restore']) is not bool:
            raise ValueError('Invalid fan threshold settings')
        triplet(fan['desired'])
        if fan['factory'] is not None:
            triplet(fan['factory'])
        if fan['restore'] and fan['factory'] is None:
            raise ValueError(f'{name}: record known factory thresholds before enabling restoration')
    return copy.deepcopy(c)


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / 'config.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self):
        return validate(json.loads(self.path.read_text())) if self.path.exists() else defaults()

    def save(self, config):
        config = validate(config)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.config-')
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump(config, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
