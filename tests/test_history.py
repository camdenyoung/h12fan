import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from h12fan.config import Store
from h12fan.controller import Controller
from h12fan.ipmi import DemoIPMI


class Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.controller = Controller(DemoIPMI(), self.store)

    def test_history_restores_order_levels_and_timestamps_after_restart(self):
        self.controller.event('First event')
        self.controller.event('Second event', 'error')
        expected = self.controller.snapshot()['events']
        restarted = Controller(DemoIPMI(), Store(self.directory.name))
        self.assertEqual(restarted.snapshot()['events'], expected)
        self.assertEqual(restarted.events[0]['message'], 'Second event')
        self.assertEqual(restarted.events[0]['level'], 'error')

    def test_only_latest_100_events_retained_on_disk_and_after_restart(self):
        for index in range(105):
            self.controller.event(f'Event {index}')
        restarted = Controller(DemoIPMI(), Store(self.directory.name))
        self.assertEqual(len(restarted.events), 100)
        self.assertEqual(restarted.events[0]['message'], 'Event 104')
        self.assertEqual(restarted.events[-1]['message'], 'Event 5')
        self.assertEqual(len(json.loads((Path(self.directory.name) / 'activity.json').read_text())), 100)

    def test_failed_atomic_replace_preserves_saved_history_and_cleans_temp_file(self):
        self.controller.event('Saved event')
        path = Path(self.directory.name) / 'activity.json'
        before = path.read_bytes()
        with patch('h12fan.config.os.replace', side_effect=OSError('Disk unavailable')):
            self.controller.event('New event')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(Path(self.directory.name).glob('.activity-*')), [])
        self.assertEqual(self.controller.events[0]['level'], 'error')
        self.controller.event('Storage works again')
        self.assertEqual(self.store.load_events(), list(self.controller.events))
        self.assertIn('New event', [e['message'] for e in self.store.load_events()])

    def test_storage_failure_does_not_interrupt_fan_control(self):
        self.controller.config.update(enabled=True, startup_seconds=0)
        with patch.object(self.store, 'save_events', side_effect=OSError('Disk full')):
            self.controller.tick()
        self.assertEqual(self.controller.state['phase'], 'controlling')
        self.assertTrue(any('duty updated:' in e['message'] for e in self.controller.events))
        warnings = [e for e in self.controller.events if 'could not be saved' in e['message']]
        self.assertEqual(len(warnings), 1)

    def test_corrupt_history_does_not_prevent_startup_or_sensor_polling(self):
        (Path(self.directory.name) / 'activity.json').write_text('{broken json')
        restarted = Controller(DemoIPMI(), Store(self.directory.name))
        self.assertIn('could not be read', restarted.events[0]['message'])
        restarted.config['startup_seconds'] = 0
        restarted.tick()
        self.assertEqual(restarted.state['phase'], 'monitoring')
        self.assertTrue(restarted.state['sensors'])
        self.assertEqual(self.store.load_events(), list(restarted.events))

    def test_duplicate_events_are_not_resaved(self):
        self.controller.event('Same event')
        with patch.object(self.store, 'save_events') as save:
            self.controller.event('Same event')
            save.assert_not_called()
        self.assertEqual(len(self.controller.events), 1)

    def test_demo_and_live_history_stay_separate(self):
        self.controller.event('Live event')
        demo = Controller(DemoIPMI(), Store(Path(self.directory.name) / 'demo'))
        demo.event('Demo event')
        self.assertEqual(self.store.load_events()[0]['message'], 'Live event')
        self.assertEqual(demo.store.load_events()[0]['message'], 'Demo event')
