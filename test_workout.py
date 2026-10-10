import unittest
import xml.etree.ElementTree as ET
import os
import tempfile
import shutil
import datetime
import http.client
import json
import subprocess
import sys
import threading

from workout import (
    WorkoutServer,
    local_network_addresses,
    _sampled_points,
    build_chart_plan,
    build_session_xml,
    create_profile,
    read_profiles,
    read_presets,
    save_profile_preset,
    save_preset,
    update_profile_watch,
    update_profile_max_pulse
)
from liveplan import session_steps
from bleheartrate import (
    HeartRateMonitor,
    _HeartRateWorker,
    parse_heart_rate_measurement,
    read_shared_json
)
from ergometer import Ergometer
from devices import ErgometerDevice, create_device, device_type_ids, public_device_types
from kettler import Kettler
from simulator import Simulator
from session import Session


class WorkoutXmlTests(unittest.TestCase):
    def test_builds_pulse_and_watt_events_with_relative_durations(self):
        xml = build_session_xml({
            'max_pulse': 180,
            'blocks': [
                {'type': 'interval', 'percent': 75, 'minutes': 5},
                {'type': 'break', 'power': 60, 'minutes': 2},
                {'type': 'interval', 'percent': 85, 'minutes': 3}
            ]
        })
        root = ET.fromstring(xml)
        events = root.find('timeEvents').findall('timeEvent')

        self.assertEqual(root.attrib['duration'], '600s')
        self.assertEqual(
            [(event.attrib['time'],
              event.find('action').attrib['type'],
              event.find('action').attrib['value']) for event in events],
            [('0', 'pulse', '135'), ('+300s', 'power', '60'),
             ('+120s', 'pulse', '153')])

    def test_blocks_can_last_half_a_minute(self):
        xml = build_session_xml({
            'max_pulse': 180,
            'blocks': [
                {'type': 'interval', 'percent': 75, 'minutes': 0.5},
                {'type': 'break', 'power': 60, 'minutes': 1.5},
                {'type': 'interval', 'percent': 85, 'minutes': 1}
            ]
        })
        root = ET.fromstring(xml)
        events = root.find('timeEvents').findall('timeEvent')

        self.assertEqual(root.attrib['duration'], '180s')
        self.assertEqual([event.attrib['time'] for event in events], ['0', '+30s', '+90s'])
        chart = build_chart_plan({
            'max_pulse': 180,
            'blocks': [{'type': 'interval', 'percent': 75, 'minutes': 0.5}]
        })
        self.assertEqual(chart['duration'], 30)

    def test_chart_plan_keeps_the_workout_name(self):
        plan = {'max_pulse': 180, 'name': '  HIIT 30/30  ',
                'blocks': [{'type': 'interval', 'percent': 75, 'minutes': 1}]}

        self.assertEqual(build_chart_plan(plan)['name'], 'HIIT 30/30')
        self.assertNotIn('name', build_chart_plan(dict(plan, name='  ')))
        self.assertNotIn('name', build_chart_plan(dict(plan, name=42)))

    def test_rejects_durations_outside_half_minute_steps(self):
        for minutes in (0, 0.25, 1.2, 720.5, True, '1'):
            with self.assertRaises(ValueError):
                build_session_xml({
                    'max_pulse': 180,
                    'blocks': [{'type': 'interval', 'percent': 70, 'minutes': minutes}]
                })

    def test_calculates_max_pulse_from_age(self):
        xml = build_session_xml({
            'max_pulse_source': 'age',
            'age': 30,
            'blocks': [{'type': 'interval', 'percent': 70, 'minutes': 5}]
        })
        root = ET.fromstring(xml)
        pulse = root.find('timeEvents').find('timeEvent').find('action').attrib['value']

        self.assertEqual(pulse, '131')

    def test_rejects_manual_max_pulse_outside_ten_bpm_steps(self):
        with self.assertRaises(ValueError):
            build_session_xml({
                'max_pulse': 181,
                'blocks': [{'type': 'interval', 'percent': 70, 'minutes': 5}]
            })

    def test_rejects_age_outside_supported_range(self):
        with self.assertRaises(ValueError):
            build_session_xml({
                'max_pulse_source': 'age',
                'age': 0,
                'blocks': [{'type': 'interval', 'percent': 70, 'minutes': 5}]
            })

    def test_requires_at_least_one_block(self):
        with self.assertRaises(ValueError):
            build_session_xml({'max_pulse': 180, 'blocks': []})

    def test_rejects_target_above_max_pulse(self):
        with self.assertRaises(ValueError):
            build_session_xml({
                'max_pulse': 180,
                'blocks': [{'type': 'interval', 'percent': 101, 'minutes': 5}]
            })

    def test_rejects_uncontrollable_break_load(self):
        with self.assertRaises(ValueError):
            build_session_xml({
                'max_pulse': 180,
                'blocks': [{'type': 'break', 'power': 0, 'minutes': 2}]
            })

    def test_rejects_break_load_not_supported_by_the_ergometer(self):
        with self.assertRaises(ValueError):
            build_session_xml({
                'max_pulse': 180,
                'blocks': [{'type': 'break', 'power': 62, 'minutes': 2}]
            })

    def test_rejects_total_duration_over_twelve_hours(self):
        with self.assertRaises(ValueError):
            build_session_xml({
                'max_pulse': 180,
                'blocks': [
                    {'type': 'interval', 'percent': 70, 'minutes': 720},
                    {'type': 'break', 'power': 60, 'minutes': 1}
                ]
            })

    def test_builds_repeated_thirty_second_intervals_and_recovery_targets(self):
        xml = build_session_xml({
            'max_pulse': 180,
            'blocks': [
                {'type': 'interval', 'percent': 55, 'minutes': 4},
                {
                    'type': 'repeat',
                    'count': 13,
                    'work_seconds': 30,
                    'work_percent': 85,
                    'recovery_seconds': 30,
                    'recovery_percent': 60
                },
                {'type': 'break', 'power': 25, 'minutes': 3}
            ]
        })
        root = ET.fromstring(xml)
        events = root.find('timeEvents').findall('timeEvent')

        self.assertEqual(root.attrib['duration'], '1200s')
        self.assertEqual(len(events), 28)
        self.assertEqual(events[0].attrib['time'], '0')
        self.assertEqual(events[1].attrib['time'], '+240s')
        self.assertEqual(events[1].find('action').attrib, {'type': 'pulse', 'value': '153'})
        self.assertEqual(events[2].attrib['time'], '+30s')
        self.assertEqual(events[2].find('action').attrib, {'type': 'pulse', 'value': '108'})
        self.assertEqual(events[-1].attrib['time'], '+30s')
        self.assertEqual(events[-1].find('action').attrib, {'type': 'power', 'value': '25'})

    def test_rejects_repeat_count_outside_supported_range(self):
        with self.assertRaises(ValueError):
            build_session_xml({
                'max_pulse': 180,
                'blocks': [{
                    'type': 'repeat',
                    'count': 0,
                    'work_seconds': 30,
                    'work_percent': 85,
                    'recovery_seconds': 30,
                    'recovery_percent': 60
                }]
            })

    def test_repeated_interval_timing_reaches_controller_at_exact_seconds(self):
        xml = build_session_xml({
            'max_pulse': 180,
            'blocks': [
                {'type': 'interval', 'percent': 55, 'minutes': 4},
                {
                    'type': 'repeat',
                    'count': 13,
                    'work_seconds': 30,
                    'work_percent': 85,
                    'recovery_seconds': 30,
                    'recovery_percent': 60
                },
                {'type': 'break', 'power': 25, 'minutes': 3}
            ]
        })
        directory = tempfile.mkdtemp(prefix='pyergometer-hiit-test-')
        path = os.path.join(directory, 'hiit.xml')
        try:
            with open(path, 'wb') as session_file:
                session_file.write(xml)
            session = Session(path)
        finally:
            shutil.rmtree(directory)

        self.assertEqual(session.duration, 1200)
        self.assertEqual(session.step(239)['pulse'], 99)
        self.assertEqual(session.step(240)['pulse'], 153)
        self.assertEqual(session.step(269)['pulse'], 153)
        self.assertEqual(session.step(270)['pulse'], 108)
        self.assertEqual(session.step(299)['pulse'], 108)
        self.assertEqual(session.step(300)['pulse'], 153)
        self.assertEqual(session.step(1019)['pulse'], 108)
        self.assertEqual(session.step(1020)['power'], 25)


class WorkoutPresetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='pyergometer-test-')
        self.path = os.path.join(self.directory, 'workouts.json')
        self.plan = {
            'max_pulse_source': 'age',
            'age': 30,
            'blocks': [
                {'type': 'interval', 'percent': 75, 'minutes': 5},
                {'type': 'break', 'power': 60, 'minutes': 2}
            ],
            'device': 'COM3',
            'simulate': False
        }

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_saves_only_ordered_blocks_without_max_pulse(self):
        presets = save_preset(self.path, 'Intervals', self.plan)

        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0]['plan'], {
            'blocks': [
                {'type': 'interval', 'percent': 75, 'minutes': 5},
                {'type': 'break', 'power': 60, 'minutes': 2}
            ]
        })
        self.assertEqual(read_presets(self.path), presets)

    def test_saving_name_again_updates_existing_preset(self):
        save_preset(self.path, 'Intervals', self.plan)
        changed_plan = dict(self.plan, blocks=[{'type': 'interval', 'percent': 80, 'minutes': 3}])

        presets = save_preset(self.path, ' intervals ', changed_plan)

        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0]['name'], 'intervals')
        self.assertEqual(read_presets(self.path)[0]['plan']['blocks'][0]['percent'], 80)

    def test_preset_without_max_pulse_is_accepted(self):
        presets = save_preset(self.path, 'Intervals', {'blocks': self.plan['blocks']})

        self.assertEqual(presets[0]['plan'], {'blocks': self.plan['blocks']})

    def test_rejects_empty_preset_name(self):
        with self.assertRaises(ValueError):
            save_preset(self.path, '  ', self.plan)


class UserProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='pyergometer-profile-test-')
        self.profiles_path = os.path.join(self.directory, 'profiles.json')
        self.presets_path = os.path.join(self.directory, 'workouts.json')
        self.plan = {
            'max_pulse_source': 'manual',
            'max_pulse': 180,
            'blocks': [{'type': 'interval', 'percent': 70, 'minutes': 5}]
        }

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_migrates_existing_workouts_to_standard_profile(self):
        save_preset(self.presets_path, 'Old plan', self.plan)

        profiles = read_profiles(self.profiles_path, self.presets_path)

        self.assertEqual([profile['name'] for profile in profiles], ['Standard'])
        self.assertEqual(profiles[0]['presets'][0]['name'], 'Old plan')
        self.assertIsNone(profiles[0]['watch'])

    def test_profiles_keep_watch_and_workouts_separate(self):
        create_profile(self.profiles_path, self.presets_path, 'Anna')
        create_profile(self.profiles_path, self.presets_path, 'Peter')
        update_profile_watch(
            self.profiles_path, self.presets_path, 'Anna',
            {'name': 'Anna watch', 'address': 'AA:BB:CC:DD:EE:FF'})
        save_profile_preset(
            self.profiles_path, self.presets_path, 'Anna', 'Intervals', self.plan)
        save_profile_preset(
            self.profiles_path, self.presets_path, 'Peter', 'Recovery', self.plan)

        profiles = read_profiles(self.profiles_path, self.presets_path)

        self.assertEqual(
            [(profile['name'], profile['watch'], [p['name'] for p in profile['presets']])
             for profile in profiles],
            [('Standard', None, []),
             ('Anna', {'name': 'Anna watch', 'address': 'AA:BB:CC:DD:EE:FF'}, ['Intervals']),
             ('Peter', None, ['Recovery'])])

    def test_max_pulse_is_stored_on_the_profile(self):
        create_profile(self.profiles_path, self.presets_path, 'Anna')
        create_profile(self.profiles_path, self.presets_path, 'Peter')
        update_profile_max_pulse(self.profiles_path, self.presets_path, 'Anna',
                                 {'source': 'manual', 'value': 180})
        update_profile_max_pulse(self.profiles_path, self.presets_path, 'Peter',
                                 {'source': 'birth_year', 'birth_year': 1970})

        profiles = {profile['name']: profile
                    for profile in read_profiles(self.profiles_path, self.presets_path)}

        self.assertIsNone(profiles['Standard']['max_pulse'])
        self.assertEqual(profiles['Anna']['max_pulse'], {'source': 'manual', 'value': 180})
        self.assertEqual(profiles['Peter']['max_pulse'],
                         {'source': 'birth_year', 'birth_year': 1970})

    def test_rejects_invalid_profile_max_pulse(self):
        create_profile(self.profiles_path, self.presets_path, 'Anna')
        for max_pulse in ({'source': 'manual', 'value': 185},
                          {'source': 'manual', 'value': 90},
                          {'source': 'birth_year', 'birth_year': 1800},
                          {'source': 'birth_year', 'birth_year': datetime.date.today().year},
                          {'source': 'age', 'age': 40},
                          None):
            with self.assertRaises(ValueError):
                update_profile_max_pulse(
                    self.profiles_path, self.presets_path, 'Anna', max_pulse)

    def test_old_presets_lose_their_max_pulse_when_read(self):
        with open(self.profiles_path, 'w') as profile_file:
            json.dump({'profiles': [{'name': 'Anna', 'watch': None, 'presets': [
                {'name': 'Old', 'plan': self.plan}]}]}, profile_file)

        profiles = read_profiles(self.profiles_path, self.presets_path)

        self.assertIsNone(profiles[0]['max_pulse'])
        self.assertEqual(profiles[0]['presets'][0]['plan'], {'blocks': self.plan['blocks']})

    def test_rejects_duplicate_profile_names(self):
        create_profile(self.profiles_path, self.presets_path, 'Anna')

        with self.assertRaises(ValueError):
            create_profile(self.profiles_path, self.presets_path, ' anna ')

    def test_watch_can_be_removed_from_a_profile(self):
        create_profile(self.profiles_path, self.presets_path, 'Anna')
        update_profile_watch(
            self.profiles_path, self.presets_path, 'Anna',
            {'name': 'Anna watch', 'address': 'AA:BB:CC:DD:EE:FF'})

        update_profile_watch(self.profiles_path, self.presets_path, 'Anna', None)

        profiles = read_profiles(self.profiles_path, self.presets_path)
        self.assertIsNone(profiles[1]['watch'])

    def test_rejects_saving_watch_for_unknown_profile(self):
        with self.assertRaises(ValueError):
            update_profile_watch(
                self.profiles_path, self.presets_path, 'Unknown',
                {'name': 'Watch', 'address': 'AA:BB:CC:DD:EE:FF'})

    def test_shared_template_name_cannot_be_saved_to_a_user_profile(self):
        with self.assertRaisesRegex(ValueError, 'new name'):
            save_profile_preset(
                self.profiles_path, self.presets_path, 'Standard',
                'HIIT 30/30 (13 gentagelser)', self.plan)

    def test_repeat_group_can_be_saved_in_a_profile(self):
        plan = {
            'max_pulse': 180,
            'blocks': [{
                'type': 'repeat',
                'count': 13,
                'work_seconds': 30,
                'work_percent': 85,
                'recovery_seconds': 30,
                'recovery_percent': 60
            }]
        }

        presets = save_profile_preset(
            self.profiles_path, self.presets_path, 'Standard', 'My HIIT', plan)

        self.assertEqual(presets[0]['plan']['blocks'], plan['blocks'])


class WorkoutChartTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            'max_pulse_source': 'manual',
            'max_pulse': 180,
            'blocks': [
                {'type': 'interval', 'percent': 75, 'minutes': 5},
                {'type': 'break', 'power': 60, 'minutes': 2},
                {'type': 'interval', 'percent': 85, 'minutes': 3}
            ]
        }

    def test_builds_chart_bars_for_intervals_and_pauses(self):
        chart = build_chart_plan(self.plan)

        self.assertEqual(chart['duration'], 600)
        self.assertEqual(chart['blocks'], [
            {'type': 'interval', 'index': 0, 'start': 0, 'end': 300, 'pulse': 135, 'percent': 75},
            {'type': 'break', 'index': 1, 'start': 300, 'end': 420, 'power': 60},
            {'type': 'interval', 'index': 2, 'start': 420, 'end': 600, 'pulse': 153, 'percent': 85}
        ])

    def test_expands_repeat_group_into_chart_intervals(self):
        chart = build_chart_plan({
            'max_pulse': 180,
            'blocks': [
                {'type': 'interval', 'percent': 55, 'minutes': 4},
                {
                    'type': 'repeat',
                    'count': 13,
                    'work_seconds': 30,
                    'work_percent': 85,
                    'recovery_seconds': 30,
                    'recovery_percent': 60
                },
                {'type': 'break', 'power': 25, 'minutes': 3}
            ]
        })

        self.assertEqual(chart['duration'], 1200)
        self.assertEqual(len(chart['blocks']), 28)
        self.assertEqual(chart['blocks'][1]['start'], 240)
        self.assertEqual(chart['blocks'][1]['end'], 270)
        self.assertEqual(chart['blocks'][1]['pulse'], 153)
        self.assertFalse(chart['blocks'][1]['draggable'])
        self.assertEqual(chart['blocks'][-1]['start'], 1020)
        self.assertEqual(chart['blocks'][-1]['end'], 1200)

    def test_demo_produces_synthetic_pulse_measurements_over_time(self):
        server = WorkoutServer(('127.0.0.1', 0))
        try:
            server.start_demo(self.plan)
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            server.demo_started_at = now - datetime.timedelta(seconds=4)
            server.advance_demo()

            self.assertEqual(server.demo_points[0], {'elapsed': 0, 'pulse': 82})
            self.assertEqual(server.demo_points[-1]['elapsed'], 4)
            self.assertGreater(server.demo_points[-1]['pulse'], 82)
        finally:
            server.server_close()

    def test_downsamples_long_measurement_series_and_keeps_final_point(self):
        points = [{'elapsed': value, 'pulse': 100 + value % 30} for value in range(5000)]

        sampled = _sampled_points(points, limit=600)

        self.assertLessEqual(len(sampled), 600)
        self.assertEqual(sampled[0], points[0])
        self.assertEqual(sampled[-1], points[-1])


class LivePlanTests(unittest.TestCase):
    def test_builds_one_control_step_per_second_for_each_block(self):
        steps = session_steps({
            'duration': 180,
            'blocks': [
                {'type': 'interval', 'start': 0, 'end': 120, 'pulse': 135},
                {'type': 'break', 'start': 120, 'end': 180, 'power': 60}
            ]
        })

        self.assertEqual(len(steps), 180)
        self.assertEqual(steps[0], {'time': 0, 'power': 0, 'pulse': 135})
        self.assertEqual(steps[119], {'time': 119, 'power': 0, 'pulse': 135})
        self.assertEqual(steps[120], {'time': 120, 'power': 60, 'pulse': 0})
        self.assertEqual(steps[-1], {'time': 179, 'power': 60, 'pulse': 0})

    def test_rejects_gaps_or_duration_mismatch_in_live_plan(self):
        with self.assertRaises(ValueError):
            session_steps({
                'duration': 180,
                'blocks': [
                    {'type': 'interval', 'start': 0, 'end': 120, 'pulse': 135},
                    {'type': 'break', 'start': 121, 'end': 180, 'power': 60}
                ]
            })


class LiveUpdateApiTests(unittest.TestCase):
    class RunningProcess(object):
        def poll(self):
            return None

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='pyergometer-live-test-')
        self.control_path = os.path.join(self.directory, 'control.jsonl')
        with open(self.control_path, 'wb'):
            pass
        self.server = WorkoutServer(('127.0.0.1', 0))
        self.server.profiles_file = os.path.join(self.directory, 'profiles.json')
        self.server.presets_file = os.path.join(self.directory, 'workouts.json')
        self.server.process = self.RunningProcess()
        self.server.chart_control_path = self.control_path
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        shutil.rmtree(self.directory)

    def post_update(self, plan):
        return self.post_json('/api/update', plan)

    def post_json(self, path, data):
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.server_port, timeout=5)
        body = json.dumps(data)
        connection.request('POST', path, body=body, headers={
            'Content-Type': 'application/json',
            'Origin': 'http://127.0.0.1:%d' % self.server.server_port
        })
        response = connection.getresponse()
        result = response.status, json.loads(response.read().decode('utf-8'))
        connection.close()
        return result

    def get_json(self, path):
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.server_port, timeout=5)
        connection.request('GET', path)
        response = connection.getresponse()
        result = response.status, json.loads(response.read().decode('utf-8'))
        connection.close()
        return result

    def test_profile_api_keeps_watch_and_presets_with_the_selected_user(self):
        status, response = self.post_json(
            '/api/profiles', {'action': 'create', 'name': 'Anna'})
        self.assertEqual(status, 200)
        status, response = self.post_json(
            '/api/profiles', {'action': 'save-watch', 'profile': 'Anna',
                              'watch': {'name': 'Anna watch',
                                        'address': 'AA:BB:CC:DD:EE:FF'}})
        self.assertEqual(status, 200)
        status, response = self.post_json('/api/presets', {
            'profile': 'Anna',
            'name': 'Intervals',
            'plan': {
                'max_pulse_source': 'manual',
                'max_pulse': 180,
                'blocks': [{'type': 'interval', 'percent': 70, 'minutes': 5}]
            }
        })
        self.assertEqual(status, 200)
        status, response = self.get_json('/api/profiles')

        anna = next(profile for profile in response['profiles']
                    if profile['name'] == 'Anna')
        self.assertEqual(anna['watch']['address'], 'AA:BB:CC:DD:EE:FF')
        self.assertEqual([preset['name'] for preset in anna['presets']], ['Intervals'])

        status, response = self.get_json('/api/presets?profile=Standard')
        self.assertEqual(status, 200)
        self.assertEqual(response['presets'], [])

    def test_sends_validated_plan_to_the_active_workout(self):
        plan = {
            'max_pulse_source': 'manual',
            'max_pulse': 180,
            'blocks': [{'type': 'interval', 'percent': 80, 'minutes': 4}]
        }

        status, response = self.post_update(plan)
        with open(self.control_path, 'rb') as control_file:
            sent_plan = json.loads(control_file.readline().decode('utf-8'))

        self.assertEqual(status, 200)
        self.assertTrue(response['updated'])
        self.assertEqual(sent_plan, self.server.chart_plan)
        self.assertEqual(sent_plan['blocks'][0]['pulse'], 144)

    def test_stop_command_is_sent_once_to_the_running_workout(self):
        status, response = self.post_json('/api/stop', {})
        with open(self.control_path, 'rb') as control_file:
            command = json.loads(control_file.readline().decode('utf-8'))

        self.assertEqual(status, 200)
        self.assertTrue(response['stopping'])
        self.assertTrue(self.server.stop_requested)
        self.assertEqual(command, {'stop': True})

    def test_rejects_stop_when_no_workout_is_running(self):
        self.server.process = None

        status, response = self.post_json('/api/stop', {})

        self.assertEqual(status, 409)
        self.assertIn('No workout', response['error'])

    def test_resets_chart_data_when_switching_profiles(self):
        self.server.process = None
        self.server.chart_plan = {'duration': 60, 'blocks': []}
        self.server.chart_live_path = 'old-workout.jsonl'
        self.server.chart_points = [{'elapsed': 30, 'pulse': 120}]
        self.server.demo_started_at = datetime.datetime.now()

        status, response = self.post_json('/api/chart/reset', {})

        self.assertEqual(status, 200)
        self.assertTrue(response['reset'])
        self.assertIsNone(self.server.chart_plan)
        self.assertIsNone(self.server.chart_live_path)
        self.assertEqual(self.server.chart_points, [])
        self.assertIsNone(self.server.demo_started_at)

    def test_rejects_chart_reset_during_a_running_workout(self):
        status, response = self.post_json('/api/chart/reset', {})

        self.assertEqual(status, 409)
        self.assertIn('during a workout', response['error'])

    def test_rejects_invalid_live_plan_without_writing_control_message(self):
        plan = {
            'max_pulse_source': 'manual',
            'max_pulse': 180,
            'blocks': [{'type': 'interval', 'percent': 101, 'minutes': 4}]
        }

        status, response = self.post_update(plan)
        with open(self.control_path, 'rb') as control_file:
            content = control_file.read()

        self.assertEqual(status, 400)
        self.assertIn('between 1 and 100', response['error'])
        self.assertEqual(content, b'')


class DeviceDriverTests(unittest.TestCase):
    def test_registry_lists_kettler_and_simulator(self):
        self.assertEqual(device_type_ids(), ['kettler', 'simulator'])
        self.assertEqual([entry['needs_port'] for entry in public_device_types()], [True, False])
        self.assertNotIn('create', public_device_types()[0])

    def test_drivers_share_the_device_interface(self):
        self.assertTrue(issubclass(Kettler, ErgometerDevice))
        self.assertTrue(issubclass(Simulator, ErgometerDevice))
        simulator = create_device('simulator')
        simulator.status()
        simulator.set_power(absolute=120)
        self.assertEqual(simulator.power, 120)

    def test_create_device_rejects_unknown_type_and_missing_port(self):
        with self.assertRaises(ValueError):
            create_device('peloton')
        with self.assertRaises(ValueError):
            create_device('kettler', None)

    def test_page_offers_the_device_types(self):
        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('GET', '/')
            page = connection.getresponse().read().decode('utf-8')
            connection.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertNotIn('@@', page)
        self.assertIn('"id": "kettler"', page)
        self.assertIn('deviceTypeSelect.value = "simulator";', page)


class NetworkOriginTests(unittest.TestCase):
    def start_server(self, host):
        server = WorkoutServer((host, 0))
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()
        self.addCleanup(stop)
        return server

    def post_profiles(self, server, origin):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        connection.request('POST', '/api/profiles', body=json.dumps({'action': 'unknown'}),
                           headers={'Content-Type': 'application/json', 'Origin': origin})
        status = connection.getresponse().status
        connection.close()
        return status

    def test_loopback_server_only_accepts_the_local_page(self):
        server = self.start_server('127.0.0.1')
        port = server.server_port
        self.assertNotEqual(self.post_profiles(server, 'http://localhost:%d' % port), 403)
        self.assertNotEqual(self.post_profiles(server, 'http://127.0.0.1:%d' % port), 403)
        for address in local_network_addresses():
            if not address.startswith('127.'):
                self.assertEqual(
                    self.post_profiles(server, 'http://%s:%d' % (address, port)), 403)

    def test_network_server_accepts_its_own_addresses_but_not_other_sites(self):
        server = self.start_server('0.0.0.0')
        port = server.server_port
        self.assertNotEqual(self.post_profiles(server, 'http://127.0.0.1:%d' % port), 403)
        for address in local_network_addresses():
            self.assertNotEqual(
                self.post_profiles(server, 'http://%s:%d' % (address, port)), 403)
        self.assertEqual(self.post_profiles(server, 'http://evil.example:%d' % port), 403)
        self.assertEqual(self.post_profiles(server, 'http://127.0.0.1:%d' % (port + 1)), 403)
        self.assertEqual(self.post_profiles(server, 'https://127.0.0.1:%d' % port), 403)
        self.assertEqual(self.post_profiles(server, 'null'), 403)


class BluetoothHeartRateTests(unittest.TestCase):
    def test_pulse_file_can_be_read_while_the_worker_replaces_it(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-shared-test-')
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, 'hr.json')
        worker = _HeartRateWorker('AA:BB:CC:DD:EE:FF', path, path + '.status')
        worker.connected = True
        worker._write_sample(70)
        stop = threading.Event()
        writer_errors = []

        def keep_writing():
            pulse = 70
            while not stop.is_set():
                pulse = 70 + (pulse - 69) % 50
                try:
                    worker._write_sample(pulse)
                except OSError as error:
                    writer_errors.append(error)

        writer = threading.Thread(target=keep_writing)
        writer.start()
        try:
            for unused in range(1000):
                self.assertTrue(70 <= read_shared_json(path)['pulse'] < 120)
        finally:
            stop.set()
            writer.join()
        self.assertEqual(writer_errors, [])

    def test_parses_standard_eight_bit_heart_rate_measurement(self):
        self.assertEqual(parse_heart_rate_measurement(bytes([0, 72])), 72)

    def test_parses_standard_sixteen_bit_heart_rate_measurement(self):
        self.assertEqual(parse_heart_rate_measurement(bytes([1, 150, 0])), 150)

    def test_rejects_invalid_or_incomplete_measurements(self):
        for sample in (b'', bytes([1, 80]), bytes([0, 10]), bytes([0, 251])):
            with self.subTest(sample=sample):
                with self.assertRaises(ValueError):
                    parse_heart_rate_measurement(sample)

    def test_isolated_monitor_publishes_samples_and_connection_status(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-ble-test-')
        sample_path = os.path.join(directory, 'heart-rate.json')
        worker = _HeartRateWorker(
            'test-address', sample_path, sample_path + '.status')
        monitor = HeartRateMonitor('test-address', sample_path)
        try:
            worker.connected = True
            worker._write_sample(88)
            self.assertTrue(monitor.status()['fresh'])
            self.assertEqual(monitor.status()['pulse'], 88)

            worker.connected = False
            worker.error = 'Connection lost'
            worker._write_connection_state()
            state = monitor.status()
            self.assertFalse(state['fresh'])
            self.assertEqual(state['error'], 'Connection lost')
        finally:
            shutil.rmtree(directory)

    def test_stale_watch_connection_keeps_the_current_load(self):
        class FakeDevice(object):
            def __init__(self):
                self.pulse = 90
                self.rpm = 60
                self.power = 100
                self.power_changes = []

            def status(self):
                self.pulse = 90

            def set_power(self, **change):
                self.power_changes.append(change)

        class FakeSession(object):
            duration = 60

            def step(self, elapsed):
                return {'pulse': 130, 'power': 0}

        directory = tempfile.mkdtemp(prefix='pyergometer-heart-test-')
        sample_path = os.path.join(directory, 'heart-rate.json')
        try:
            with open(sample_path, 'w') as sample_file:
                json.dump({
                    'pulse': 150,
                    'timestamp': datetime.datetime.now().timestamp(),
                    'connected': False
                }, sample_file)
            device = FakeDevice()
            controller = Ergometer(
                device=device, session=FakeSession(), heart_rate_file=sample_path)
            controller.last_watch_pulse = 150
            controller.control_step(1)

            self.assertEqual(device.pulse, 150)
            self.assertEqual(device.power_changes, [])
        finally:
            shutil.rmtree(directory)


class Python3ControllerTests(unittest.TestCase):
    def test_parses_and_updates_session_without_lxml(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-session-test-')
        session_path = os.path.join(directory, 'session.xml')
        try:
            with open(session_path, 'wb') as session_file:
                session_file.write(
                    b'<program name="Test" duration="3s"><timeEvents>'
                    b'<timeEvent time="0"><action type="pulse" value="120"/>'
                    b'</timeEvent></timeEvents></program>')
            session = Session(session_path)
            self.assertEqual(session.duration, 3)
            self.assertEqual(session.step(0)['pulse'], 120)

            session.update_live_plan({
                'duration': 120,
                'blocks': [{'type': 'interval', 'start': 0, 'end': 120, 'pulse': 140}]
            })
            self.assertEqual(session.duration, 120)
            self.assertEqual(session.step(119)['pulse'], 140)
        finally:
            shutil.rmtree(directory)

    def test_stop_command_is_read_by_the_controller(self):
        class FakeSession(object):
            def update_live_plan(self, plan):
                raise AssertionError('stop command must not be parsed as a plan')

        directory = tempfile.mkdtemp(prefix='pyergometer-stop-test-')
        control_path = os.path.join(directory, 'control.jsonl')
        try:
            with open(control_path, 'wb') as control_file:
                control_file.write(b'{"stop":true}\n')
            controller = Ergometer(
                device=object(), session=FakeSession(), live_control=control_path)

            controller.poll_live_updates()

            self.assertTrue(controller.stop_requested)
        finally:
            shutil.rmtree(directory)

    def test_finish_sets_ergometer_to_minimum_load(self):
        class FakeDevice(object):
            pulse = 120
            power = 100
            rpm = 50

            def __init__(self):
                self.power_changes = []

            def set_power(self, **change):
                self.power_changes.append(change)

        class FakeSession(object):
            def step(self, unused_second):
                return {'pulse': 120, 'power': 0}

        device = FakeDevice()
        controller = Ergometer(device=device, session=FakeSession())
        controller.pc = 0

        controller.finish()

        self.assertEqual(device.power_changes, [{'absolute': 25}])

    def test_converts_workout_log_to_csv_under_python_three(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-csv-test-')
        log_path = os.path.join(directory, 'workout.json')
        try:
            with open(log_path, 'w') as log_file:
                json.dump({
                    'begin': '2026-10-06T12:00:00',
                    'steps': [{
                        'pulse': '120',
                        'target_pulse': '130',
                        'power': '60',
                        'rpm': '50'
                    }]
                }, log_file)
            result = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), 'dumplog.py'),
                 log_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [
                'Time,Power (W),Rpm,Pulse,Target pulse',
                '2026-10-06T12:00:00,60,50,120,130'
            ])
        finally:
            shutil.rmtree(directory)


if __name__ == '__main__':
    unittest.main()
