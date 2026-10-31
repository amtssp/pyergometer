import unittest
import xml.etree.ElementTree as ET
import os
import tempfile
import shutil
import datetime
import http.client
import json
import re
import subprocess
import sys
import threading
import time

from workout import (
    WorkoutServer,
    local_network_addresses,
    _sampled_points,
    normalize_preset_plan,
    summarize_workout,
    analyze_astrand,
    analyze_ramp,
    append_history,
    change_history,
    read_history,
    rescore_test,
    astrand_age_factor,
    astrand_vo2max_litres,
    update_profile_details,
    pulse_limit,
    build_chart_plan,
    build_session_xml,
    create_profile,
    read_profiles,
    read_presets,
    save_profile_preset,
    save_preset,
    update_profile_watch,
    update_profile_max_pulse,
    read_bike_settings,
    write_bike_settings,
    builtin_template_names,
    read_languages
)
from liveplan import session_steps, skip_to_recovery
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
        self.assertEqual(steps[0], {'time': 0, 'power': 0, 'pulse': 135, 'recovery': False})
        self.assertEqual(steps[119], {'time': 119, 'power': 0, 'pulse': 135, 'recovery': False})
        self.assertEqual(steps[120], {'time': 120, 'power': 60, 'pulse': 0, 'recovery': False})
        self.assertEqual(steps[-1], {'time': 179, 'power': 60, 'pulse': 0, 'recovery': False})

    def ramp_test_plan(self):
        return build_chart_plan({'mode': 'power', 'blocks': [
            {'type': 'interval', 'minutes': 5, 'power': 50},
            {'type': 'ramp', 'minutes': 30, 'start_power': 50, 'end_power': 650, 'test': 'ramp'},
            {'type': 'recovery', 'seconds': 60}]})

    def test_stop_moves_the_pulse_drop_forward(self):
        plan = self.ramp_test_plan()

        skipped = skip_to_recovery(plan, 900)

        self.assertEqual([(b['type'], b['start'], b['end']) for b in skipped['blocks']],
                         [('power', 0, 300), ('ramp', 300, 900), ('recovery', 900, 960)])
        # The ramp keeps its slope of 20 W per minute; it only stops early.
        self.assertEqual((skipped['blocks'][1]['start_value'], skipped['blocks'][1]['end_value']),
                         (50, 250))
        self.assertEqual(skipped['duration'], 960)
        steps = session_steps(skipped)
        self.assertEqual((steps[899]['power'], steps[899]['test']), (250, 'ramp'))
        self.assertTrue(steps[900]['recovery'])
        # During the pulse drop, or without one, Stop really stops.
        self.assertIsNone(skip_to_recovery(skipped, 930))
        self.assertIsNone(skip_to_recovery(build_chart_plan({'mode': 'power', 'blocks': [
            {'type': 'interval', 'minutes': 5, 'power': 50}]}), 60))

    def test_controller_jumps_to_the_pulse_drop(self):
        plan = self.ramp_test_plan()
        directory = tempfile.mkdtemp(prefix='pyergometer-skip-test-')
        self.addCleanup(shutil.rmtree, directory)
        session_path = os.path.join(directory, 'session.xml')
        with open(session_path, 'wb') as session_file:
            session_file.write(build_session_xml({'mode': 'power', 'blocks': [
                {'type': 'interval', 'minutes': 5, 'power': 50}]}))
        live_path = os.path.join(directory, 'live.jsonl')
        control_path = os.path.join(directory, 'control.jsonl')
        with open(control_path, 'w') as control_file:
            control_file.write(json.dumps(plan) + '\n')

        class Device(object):
            rpm = 80
            pulse = 170
            power = 0

            def status(self):
                pass

            def set_power(self, absolute=0.0, relative=0.0):
                self.power = absolute

        controller = Ergometer(device=Device(), session=Session(session_path),
                               live_log=live_path, live_control=control_path)
        controller.poll_live_updates()
        controller.begin_monotonic = time.monotonic() - 900
        controller.begin = datetime.datetime.utcnow()
        with open(control_path, 'a') as control_file:
            control_file.write('{"skip_to_recovery":true}\n')
        controller.poll_live_updates()
        controller.control_step(900)

        self.assertFalse(controller.stop_requested)
        self.assertTrue(controller.session.step(900)['recovery'])
        self.assertEqual(controller.session.duration, 960)
        controller.live_log_file.close()
        with open(live_path) as live_file:
            sample = json.loads(live_file.readline())
        self.assertEqual((sample['recovery'], sample['skipped_to_recovery']), (True, 900))

        # A second Stop during the pulse drop ends the workout.
        with open(control_path, 'a') as control_file:
            control_file.write('{"skip_to_recovery":true}\n')
        controller.poll_live_updates()
        self.assertTrue(controller.stop_requested)

    def test_ramp_test_jumps_to_the_pulse_drop_when_the_rider_stops_pedalling(self):
        plan = self.ramp_test_plan()

        class Device(object):
            rpm = 10
            pulse = 175
            power = 0

            def status(self):
                pass

            def set_power(self, absolute=0.0, relative=0.0):
                self.power = absolute

        class LiveSession(object):
            def __init__(self):
                self.update_live_plan(plan)

            def step(self, second):
                return self.steps[min(second, len(self.steps) - 1)]

            def update_live_plan(self, new_plan):
                self.steps = session_steps(new_plan)
                self.duration = len(self.steps)

        controller = Ergometer(device=Device(), session=LiveSession())
        controller.live_plan = plan
        for second in range(600, 604):
            controller.control_step(second)
        self.assertFalse(controller.session.step(605)['recovery'])
        controller.control_step(604)
        self.assertTrue(controller.session.step(604)['recovery'])
        self.assertEqual(controller.session.duration, 664)
        # Outside a ramp test a short stop does not end the intervals.
        warmup = Ergometer(device=Device(), session=LiveSession())
        warmup.live_plan = plan
        for second in range(10, 30):
            warmup.control_step(second)
        self.assertFalse(warmup.session.step(30)['recovery'])

    def test_server_moves_its_chart_like_the_controller(self):
        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        self.addCleanup(server.server_close)
        server.chart_plan = self.ramp_test_plan()
        server.chart_points = [{'elapsed': 899, 'pulse': 170}]

        self.assertTrue(server.stop_jumps_to_recovery())
        server.apply_skip(900)
        server.apply_skip(900)

        self.assertEqual(server.chart_skips, [900])
        self.assertEqual(server.chart_plan['duration'], 960)
        server.chart_points = [{'elapsed': 930, 'pulse': 150}]
        self.assertFalse(server.stop_jumps_to_recovery())

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

    def test_kettler_load_rises_at_once_after_time_at_the_floor(self):
        class FakePort:
            def __init__(self):
                self.written = []

            def write(self, data):
                self.written.append(data)

        bike = Kettler.__new__(Kettler)
        bike.power = 0
        bike.xpower = 0.0
        bike.serialport = FakePort()
        # A long warm-up with the pulse above target keeps asking for less load.
        for _ in range(240):
            bike.set_power(relative=-0.30)
        self.assertEqual(bike.power, 25)
        # Once the target is above the pulse, the load leaves the floor within seconds.
        for _ in range(10):
            bike.set_power(relative=0.30)
        self.assertEqual(bike.power, 30)
        self.assertEqual(bike.serialport.written[-1], b'PW 30\r\n')

    def test_create_device_rejects_unknown_type_and_missing_port(self):
        with self.assertRaises(ValueError):
            create_device('peloton')
        with self.assertRaises(ValueError):
            create_device('kettler', None)

    def test_development_page_is_served(self):
        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('GET', '/udvikling?profile=Demo')
            response = connection.getresponse()
            page = response.read().decode('utf-8')
            connection.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(response.status, 200)
        self.assertIn('data-i18n="development.title"', page)
        self.assertIn('<script src="/i18n.js"></script>', page)

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


class BikeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='pyergometer-bike-test-')
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, 'bike_settings.json')

    def test_saved_settings_are_read_back(self):
        write_bike_settings(self.path, {'device_type': 'kettler', 'device': ' COM4 '})

        self.assertEqual(read_bike_settings(self.path),
                         {'device_type': 'kettler', 'device': 'COM4'})

    def test_missing_or_broken_settings_are_ignored(self):
        self.assertIsNone(read_bike_settings(self.path))
        with open(self.path, 'w') as settings_file:
            settings_file.write('{"device_type": "peloton", "device": ""}')
        self.assertIsNone(read_bike_settings(self.path))
        with self.assertRaises(ValueError):
            write_bike_settings(self.path, {'device_type': 'peloton', 'device': ''})

    def test_server_starts_with_saved_settings_unless_overridden(self):
        write_bike_settings(self.path, {'device_type': 'simulator', 'device': 'COM4'})

        saved = WorkoutServer(('127.0.0.1', 0), bike_settings_file=self.path)
        overridden = WorkoutServer(('127.0.0.1', 0), device='/dev/ttyUSB0',
                                   device_type='kettler', bike_settings_file=self.path)
        saved.server_close()
        overridden.server_close()

        self.assertEqual((saved.device_type, saved.device), ('simulator', 'COM4'))
        self.assertEqual((overridden.device_type, overridden.device), ('kettler', '/dev/ttyUSB0'))

    def test_page_choice_is_saved_through_the_api(self):
        server = WorkoutServer(('127.0.0.1', 0), bike_settings_file=self.path)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('POST', '/api/bike-settings',
                               body=json.dumps({'device_type': 'kettler', 'device': 'COM7'}),
                               headers={'Content-Type': 'application/json',
                                        'Origin': 'http://127.0.0.1:%d' % server.server_port})
            status = connection.getresponse().status
            connection.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(status, 200)
        self.assertEqual(server.device, 'COM7')
        self.assertEqual(read_bike_settings(self.path), {'device_type': 'kettler', 'device': 'COM7'})


class ConcurrentRequestTests(unittest.TestCase):
    def test_a_slow_watch_scan_does_not_block_other_devices(self):
        import time
        import workout

        def slow_scan():
            time.sleep(2)
            return []

        original_scan = workout.scan_heart_rate_devices
        workout.scan_heart_rate_devices = slow_scan
        self.addCleanup(setattr, workout, 'scan_heart_rate_devices', original_scan)
        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()
        self.addCleanup(stop)

        def scan():
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            connection.request('POST', '/api/heart-rate/scan', body='{}', headers={
                'Content-Type': 'application/json',
                'Origin': 'http://127.0.0.1:%d' % server.server_port})
            connection.getresponse().read()
            connection.close()

        scanner = threading.Thread(target=scan)
        scanner.start()
        time.sleep(0.3)
        started = time.monotonic()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        connection.request('GET', '/api/status')
        status = connection.getresponse().status
        connection.close()
        waited = time.monotonic() - started
        scanner.join()

        self.assertEqual(status, 200)
        self.assertLess(waited, 1.0)


class StartingStateTests(unittest.TestCase):
    def test_a_start_waiting_for_the_watch_is_reported_and_blocks_another(self):
        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()
        self.addCleanup(stop)
        server.starting = True

        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        connection.request('GET', '/api/status')
        status = json.loads(connection.getresponse().read().decode('utf-8'))
        connection.request('POST', '/api/start', body=json.dumps({
            'max_pulse': 180, 'blocks': [{'type': 'interval', 'minutes': 1, 'percent': 70}]}),
            headers={'Content-Type': 'application/json',
                     'Origin': 'http://127.0.0.1:%d' % server.server_port})
        response = connection.getresponse()
        body = json.loads(response.read().decode('utf-8'))
        connection.close()

        self.assertTrue(status['starting'])
        self.assertFalse(status['running'])
        self.assertEqual(response.status, 409)
        self.assertEqual(body['code'], 'starting')
        self.assertIn('error', body)


class WatchStartTests(unittest.TestCase):
    def start(self, mode, waits):
        import glob
        from unittest import mock
        import workout

        class FakeMonitor(object):
            def __init__(self, address, path):
                pass

            def start(self):
                pass

            def wait_for_sample(self, timeout):
                waits.append(timeout)
                return False

            def status(self):
                return {'error': 'test'}

            def stop(self):
                pass

        class FakeProcess(object):
            def poll(self):
                return None

        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        blocks = ([{'type': 'interval', 'minutes': 1, 'power': 150}] if mode == 'power'
                  else [{'type': 'interval', 'minutes': 1, 'percent': 70}])
        try:
            with mock.patch.object(workout, 'HeartRateMonitor', FakeMonitor),                     mock.patch.object(workout.subprocess, 'Popen', lambda *a, **k: FakeProcess()):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                connection.request('POST', '/api/start', body=json.dumps({
                    'mode': mode, 'max_pulse': 180, 'blocks': blocks, 'device_type': 'simulator',
                    'pulse_source': 'watch', 'heart_rate_address': 'AA:BB:CC:DD:EE:FF'}),
                    headers={'Content-Type': 'application/json',
                             'Origin': 'http://127.0.0.1:%d' % server.server_port})
                response = connection.getresponse()
                body = json.loads(response.read().decode('utf-8'))
                connection.close()
        finally:
            server.process = None
            server.shutdown()
            thread.join()
            server.server_close()
            # Remove the live files a successful start leaves in data/.
            if body.get('log'):
                data = os.path.join(os.path.dirname(os.path.abspath(workout.__file__)), 'data')
                for path in glob.glob(os.path.join(data, '.' + body['log'] + '.live*')):
                    os.remove(path)
            if server.session_file and os.path.exists(server.session_file):
                os.remove(server.session_file)
        return response.status, body

    def test_watt_workout_starts_without_waiting_for_the_watch(self):
        waits = []
        status, body = self.start('power', waits)

        self.assertEqual(status, 200)
        self.assertEqual(waits, [])

    def test_pulse_workout_still_needs_the_watch(self):
        waits = []
        status, body = self.start('pulse', waits)

        self.assertEqual(status, 400)
        self.assertEqual(waits, [30])
        self.assertIn(body['code'], ('watch_no_pulse', 'watch_no_connection'))
        self.assertEqual(body['params']['seconds'], 30)


class DemoLiveUpdateTests(unittest.TestCase):
    def test_shortening_a_block_during_a_demo_moves_the_demo_on(self):
        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()
        self.addCleanup(stop)

        def post(path, plan):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('POST', path, body=json.dumps(plan), headers={
                'Content-Type': 'application/json',
                'Origin': 'http://127.0.0.1:%d' % server.server_port})
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status

        def plan(minutes):
            return {'max_pulse': 180, 'blocks': [
                {'type': 'interval', 'minutes': minutes, 'percent': 70}, {'type': 'recovery'}]}

        self.assertEqual(post('/api/demo', plan(5)), 200)
        # Pretend the demo has run for 150 seconds of the 5-minute interval.
        server.demo_started_at -= datetime.timedelta(seconds=150)
        self.assertEqual(post('/api/update', plan(2)), 200)

        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        connection.request('GET', '/api/chart')
        chart = json.loads(connection.getresponse().read().decode('utf-8'))
        connection.close()
        current = [block for block in chart['plan']['blocks']
                   if block['start'] <= chart['elapsed'] < block['end']]
        self.assertTrue(chart['active'])
        self.assertEqual(current[0]['type'], 'recovery')


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        thread = threading.Thread(target=self.server.serve_forever)
        thread.daemon = True
        thread.start()

        def stop():
            self.server.shutdown()
            thread.join()
            self.server.server_close()
        self.addCleanup(stop)

    def chart(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('GET', '/api/chart')
        chart = json.loads(connection.getresponse().read().decode('utf-8'))
        connection.close()
        return chart

    def test_nothing_to_show_without_a_workout(self):
        self.assertIsNone(self.chart()['rpm'])

    def test_a_demo_pedals_but_sits_still_during_a_pulse_drop(self):
        self.server.start_demo({'max_pulse': 180, 'blocks': [
            {'type': 'interval', 'minutes': 1, 'percent': 70}, {'type': 'recovery'}]})
        self.server.demo_started_at -= datetime.timedelta(seconds=30)
        self.assertTrue(55 <= self.chart()['rpm'] <= 75)

        self.server.demo_started_at -= datetime.timedelta(seconds=40)
        self.assertEqual(self.chart()['rpm'], 0)

    def test_a_workout_shows_the_latest_cadence_until_the_rider_stops(self):
        class Running(object):
            def poll(self):
                return None
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        live_path = os.path.join(directory, 'live.jsonl')
        with open(live_path, 'w') as live_file:
            for second, rpm in ((1, 58), (2, 61)):
                live_file.write(json.dumps({'elapsed': second, 'pulse': 120, 'rpm': rpm}) + '\n')
        self.server.chart_plan = build_chart_plan(
            {'max_pulse': 180, 'blocks': [{'type': 'interval', 'minutes': 5, 'percent': 70}]})
        self.server.chart_live_path = live_path
        self.server.process = Running()
        self.addCleanup(setattr, self.server, 'process', None)

        self.assertEqual(self.chart()['rpm'], 61)

        # No new samples: the rider has stopped pedalling.
        self.server.chart_rpm_at -= datetime.timedelta(seconds=10)
        self.assertIsNone(self.chart()['rpm'])


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


class WattWorkoutTests(unittest.TestCase):
    def events(self, plan):
        root = ET.fromstring(build_session_xml(plan))
        return root.attrib['duration'], [
            (event.attrib['time'], event.find('action').attrib['type'],
             int(event.find('action').attrib['value']))
            for event in root.find('timeEvents').findall('timeEvent')]

    def test_watt_workout_needs_no_maximum_pulse(self):
        duration, events = self.events({
            'mode': 'power',
            'blocks': [
                {'type': 'interval', 'minutes': 1, 'power': 150},
                {'type': 'repeat', 'count': 2, 'work_seconds': 30, 'work_power': 285,
                 'recovery_seconds': 30, 'recovery_power': 130}
            ]
        })

        self.assertEqual(duration, '180s')
        self.assertEqual(events, [
            ('0', 'power', 150), ('+60s', 'power', 285), ('+30s', 'power', 130),
            ('+30s', 'power', 285), ('+30s', 'power', 130)])

    def test_pulse_workout_still_needs_maximum_pulse(self):
        with self.assertRaises(ValueError):
            build_session_xml({'blocks': [{'type': 'interval', 'minutes': 1, 'percent': 70}]})

    def test_power_ramp_changes_linearly_every_second(self):
        steps = session_steps(build_chart_plan({
            'mode': 'power',
            'blocks': [{'type': 'ramp', 'minutes': 0.5, 'start_power': 100, 'end_power': 160}]
        }))

        self.assertEqual(len(steps), 30)
        self.assertEqual([step['power'] for step in steps[:3]], [100, 102, 104])
        self.assertEqual(steps[-1]['power'], 158)
        self.assertTrue(all(step['pulse'] == 0 for step in steps))

    def test_pulse_ramp_uses_percent_of_maximum_pulse(self):
        chart = build_chart_plan({
            'max_pulse': 200,
            'blocks': [{'type': 'ramp', 'minutes': 1, 'start_percent': 50, 'end_percent': 80}]
        })
        ramp = chart['blocks'][0]

        self.assertEqual((ramp['kind'], ramp['start_value'], ramp['end_value']),
                         ('pulse', 100, 160))
        self.assertEqual(session_steps(chart)[30]['pulse'], 130)

    def test_watt_loads_are_validated(self):
        for power in (24, 1001, 150.5, None):
            with self.assertRaises(ValueError):
                build_chart_plan({'mode': 'power',
                                  'blocks': [{'type': 'interval', 'minutes': 1, 'power': power}]})
        with self.assertRaises(ValueError):
            build_chart_plan({'mode': 'cadence', 'blocks': []})

    def test_vo2max_template_matches_the_erg_file(self):
        steps = session_steps(build_chart_plan({'mode': 'power', 'blocks': [
            {'type': 'ramp', 'minutes': 5.5, 'start_power': 100, 'end_power': 168},
            {'type': 'interval', 'minutes': 1.5, 'power': 220},
            {'type': 'interval', 'minutes': 2, 'power': 125},
            {'type': 'repeat', 'count': 5, 'work_seconds': 60, 'work_power': 285,
             'recovery_seconds': 75, 'recovery_power': 130},
            {'type': 'ramp', 'minutes': 8, 'start_power': 150, 'end_power': 105}
        ]}))

        self.assertEqual(len(steps), int(28.25 * 60))
        # Spot-check against the ERG file: minute 9 starts the first 285 W interval,
        # minute 20.25 starts the 150 W cool-down ramp.
        self.assertEqual(steps[9 * 60]['power'], 285)
        self.assertEqual(steps[10 * 60]['power'], 130)
        self.assertEqual(steps[int(20.25 * 60)]['power'], 150)

    def test_template_names_cannot_be_overwritten(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-template-test-')
        self.addCleanup(shutil.rmtree, directory)
        for name in ('Watt · VO₂ Max 5 × 5', 'puls · hiit 30/30 (13 gentagelser)'):
            with self.assertRaisesRegex(ValueError, 'new name'):
                save_profile_preset(
                    os.path.join(directory, 'profiles.json'),
                    os.path.join(directory, 'workouts.json'), 'Standard', name,
                    {'mode': 'power', 'blocks': [{'type': 'interval', 'minutes': 1, 'power': 150}]})

    def test_pulse_limit_only_for_watt_workouts_with_a_maximum_pulse(self):
        watt = build_chart_plan({'mode': 'power', 'max_pulse': 180,
                                 'blocks': [{'type': 'interval', 'minutes': 1, 'power': 200}]})
        no_maximum = build_chart_plan({'mode': 'power',
                                       'blocks': [{'type': 'interval', 'minutes': 1, 'power': 200}]})
        pulse = build_chart_plan({'max_pulse': 180,
                                  'blocks': [{'type': 'interval', 'minutes': 1, 'percent': 70}]})

        self.assertEqual(pulse_limit(watt), 171)
        self.assertIsNone(pulse_limit(watt, enabled=False))
        self.assertIsNone(pulse_limit(no_maximum))
        self.assertIsNone(pulse_limit(pulse))

    def test_saved_watt_workout_keeps_its_mode(self):
        plan = {'mode': 'power', 'max_pulse': 180,
                'blocks': [{'type': 'interval', 'minutes': 1, 'power': 200}]}

        self.assertEqual(normalize_preset_plan(plan),
                         {'mode': 'power', 'blocks': plan['blocks']})
        self.assertNotIn('mode', normalize_preset_plan(
            {'blocks': [{'type': 'interval', 'minutes': 1, 'percent': 70}]}))

    def make_controller(self, pulse, pulse_limit=None):
        class FakeDevice(object):
            rpm = 60
            power = 0

            def __init__(self):
                self.pulse = pulse
                self.loads = []

            def status(self):
                pass

            def set_power(self, absolute=0.0, relative=0.0):
                self.loads.append(absolute)
                self.power = absolute

        class FakeSession(object):
            def step(self, second):
                return {'time': second, 'power': 200, 'pulse': 0}

        return Ergometer(device=FakeDevice(), session=FakeSession(), pulse_limit=pulse_limit)

    def test_watt_step_is_applied_without_a_pulse_reading(self):
        controller = self.make_controller(pulse=0)

        controller.control_step(0)

        self.assertEqual(controller.device.loads, [200])

    def pulse_controller(self, pulse, target):
        class Device(object):
            rpm = 60
            power = 100

            def __init__(self):
                self.pulse = pulse
                self.changes = []

            def status(self):
                pass

            def set_power(self, absolute=0.0, relative=0.0):
                self.changes.append(relative)

        class Session(object):
            def step(self, second):
                return {'time': second, 'power': 0, 'pulse': target}

        return Ergometer(device=Device(), session=Session())

    def test_pulse_control_raises_the_load_faster_far_from_target(self):
        changes = []
        for pulse in (90, 125, 133, 137):
            controller = self.pulse_controller(pulse=pulse, target=140)
            controller.control_step(0)
            changes.extend(controller.device.changes)
        # 50, 15, 7 and 3 beats below the target.
        self.assertEqual(changes, [1.0, 0.5, 0.15, 0.03])

    def test_watch_pulse_replaces_the_bike_pulse_while_the_watch_connects(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-watch-test-')
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, 'heart-rate.json')
        with open(path, 'w') as sample_file:
            json.dump({'pulse': None, 'timestamp': None, 'connected': False}, sample_file)
        controller = self.make_controller(pulse=59)
        controller.heart_rate_file = path

        self.assertFalse(controller.refresh_heart_rate())
        self.assertEqual(controller.device.pulse, 0)

        with open(path, 'w') as sample_file:
            json.dump({'pulse': 95, 'timestamp': time.time(), 'connected': True}, sample_file)
        self.assertTrue(controller.refresh_heart_rate())
        controller.device.pulse = 59
        with open(path, 'w') as sample_file:
            json.dump({'pulse': 95, 'timestamp': time.time() - 10, 'connected': False}, sample_file)
        self.assertFalse(controller.refresh_heart_rate())
        self.assertEqual(controller.device.pulse, 95)

    def test_pulse_limit_lowers_and_restores_the_load(self):
        controller = self.make_controller(pulse=180, pulse_limit=171)
        controller.control_step(0)
        controller.control_step(1)
        controller.device.pulse = 168
        controller.control_step(2)
        controller.device.pulse = 160
        controller.control_step(3)
        controller.control_step(4)
        controller.control_step(5)
        controller.control_step(6)

        self.assertEqual(controller.device.loads, [190, 180, 180, 185, 190, 195, 200])


class FitnessDataTests(unittest.TestCase):
    def test_profile_details_are_saved_and_validated(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-details-test-')
        self.addCleanup(shutil.rmtree, directory)
        profiles_path = os.path.join(directory, 'profiles.json')
        presets_path = os.path.join(directory, 'workouts.json')
        create_profile(profiles_path, presets_path, 'Anna')
        update_profile_max_pulse(profiles_path, presets_path, 'Anna',
                                 {'source': 'birth_year', 'birth_year': 1960})

        update_profile_details(profiles_path, presets_path, 'Anna',
                               {'birth_year': 1962, 'sex': 'female', 'weight_kg': 64.25})
        anna = read_profiles(profiles_path, presets_path)[1]

        self.assertEqual((anna['birth_year'], anna['sex'], anna['weight_kg']), (1962, 'female', 64.2))
        # A maximum pulse from birth year follows the saved year.
        self.assertEqual(anna['max_pulse'], {'source': 'birth_year', 'birth_year': 1962})
        for details in ({'sex': 'other'}, {'weight_kg': 10}, {'birth_year': 1700}):
            with self.assertRaises(ValueError):
                update_profile_details(profiles_path, presets_path, 'Anna', details)

    def test_old_profiles_get_their_birth_year_from_max_pulse(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-details-test-')
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, 'profiles.json')
        with open(path, 'w') as profile_file:
            json.dump({'profiles': [{'name': 'Bo', 'watch': None, 'presets': [],
                                     'max_pulse': {'source': 'birth_year', 'birth_year': 1959}}]},
                      profile_file)

        bo = read_profiles(path)[0]

        self.assertEqual((bo['birth_year'], bo['sex'], bo['weight_kg']), (1959, None, None))

    def test_repeat_before_a_pulse_drop_leaves_out_its_last_easy_interval(self):
        repeat = {'type': 'repeat', 'count': 3, 'work_seconds': 180, 'work_percent': 85,
                  'recovery_seconds': 180, 'recovery_percent': 65}
        alone = build_chart_plan({'max_pulse': 160, 'blocks': [repeat]})
        chart = build_chart_plan({'max_pulse': 160, 'blocks': [repeat, {'type': 'recovery'}]})

        self.assertEqual(alone['duration'], 3 * 360)
        self.assertEqual([b.get('repeat_phase') or b['type'] for b in chart['blocks']],
                         ['work', 'recovery', 'work', 'recovery', 'work', 'recovery'][:5] + ['recovery'])
        self.assertEqual(chart['blocks'][4]['repeat_phase'], 'work')
        self.assertEqual((chart['blocks'][5]['type'], chart['blocks'][5]['start']), ('recovery', 900))
        self.assertEqual(chart['duration'], 960)

    def test_recovery_block_sits_still_at_minimum_load(self):
        chart = build_chart_plan({'max_pulse': 180, 'blocks': [
            {'type': 'interval', 'minutes': 1, 'percent': 80},
            {'type': 'recovery'}]})
        steps = session_steps(chart)

        self.assertEqual(chart['blocks'][1]['type'], 'recovery')
        self.assertEqual(len(steps), 120)
        self.assertEqual((steps[60]['power'], steps[60]['recovery']), (25, True))
        root = ET.fromstring(build_session_xml({'max_pulse': 180, 'blocks': [
            {'type': 'interval', 'minutes': 1, 'percent': 80}, {'type': 'recovery', 'seconds': 90}]}))
        actions = [event.find('action').attrib['type'] for event in root.find('timeEvents')]
        self.assertEqual(actions, ['pulse', 'recovery'])

    def test_controller_measures_pulse_while_the_rider_sits_still(self):
        class Device(object):
            rpm = 0
            pulse = 150
            power = 200

            def status(self):
                pass

            def set_power(self, absolute=0.0, relative=0.0):
                self.power = absolute

        class Session(object):
            def step(self, second):
                return {'time': second, 'power': 25, 'pulse': 0, 'recovery': True}

        directory = tempfile.mkdtemp(prefix='pyergometer-recovery-test-')
        self.addCleanup(shutil.rmtree, directory)
        live_path = os.path.join(directory, 'live.jsonl')
        controller = Ergometer(device=Device(), session=Session(), live_log=live_path)
        controller.control_step(5)
        controller.live_log_file.close()

        with open(live_path) as live_file:
            sample = json.loads(live_file.readline())
        self.assertEqual((sample['pulse'], sample['recovery']), (150, True))
        self.assertEqual(controller.device.power, 25)

    def test_summary_measures_pulse_drop(self):
        chart = build_chart_plan({'max_pulse': 200, 'blocks': [
            {'type': 'interval', 'minutes': 5, 'percent': 70},
            {'type': 'recovery'}]})
        samples = [{'elapsed': second, 'pulse': 140, 'power': 150} for second in range(300)]
        samples += [{'elapsed': 300 + second, 'pulse': 140 - second // 2, 'power': 25,
                     'recovery': True} for second in range(60)]

        summary = summarize_workout(samples, chart)

        self.assertEqual(summary['recoveries'], [
            {'start_pulse': 140, 'end_pulse': 111, 'drop': 29, 'seconds': 60}])
        # A single pulse interval is not one of the compared kinds of effort.
        self.assertEqual((summary['watt_per_beat'], summary['efficiency']), (None, []))
        self.assertEqual(summary['max_pulse'], 140)
        self.assertEqual(summary['avg_power'], 150)
        self.assertEqual(summary['energy_kj'], 45.0)


class EfficiencyTests(unittest.TestCase):
    def test_hard_intervals_of_a_repeat_give_watt_per_beat(self):
        chart = build_chart_plan({'max_pulse': 160, 'blocks': [
            {'type': 'interval', 'minutes': 5, 'percent': 65},
            {'type': 'repeat', 'count': 4, 'work_seconds': 240, 'work_percent': 90,
             'recovery_seconds': 180, 'recovery_percent': 70},
            {'type': 'recovery'}]})
        samples = []
        for block in chart['blocks']:
            for second in range(block['start'], block['end']):
                hard = block.get('repeat_phase') == 'work'
                # The pulse rises during the first minute of each hard interval.
                pulse = (144 if second - block['start'] >= 60 else 120) if hard else 110
                samples.append({'elapsed': second, 'pulse': pulse, 'power': 160 if hard else 90})

        readings = summarize_workout(samples, chart)['efficiency']

        self.assertEqual(readings, [{'kind': 'interval', 'seconds': 240, 'percent': 90,
                                     'watts': 160.0, 'pulse': 144.0, 'value': 1.111}])

    def test_astrand_test_gives_watt_per_beat_at_its_load(self):
        chart = build_chart_plan({'mode': 'power', 'blocks': [
            {'type': 'interval', 'minutes': 3, 'power': 50},
            {'type': 'interval', 'minutes': 6, 'power': 100, 'test': 'astrand'}]})
        samples = [{'elapsed': second, 'pulse': 100 if second < 180 else 130,
                    'power': 50 if second < 180 else 100} for second in range(540)]

        readings = summarize_workout(samples, chart)['efficiency']

        self.assertEqual(readings, [{'kind': 'astrand', 'load': 100, 'watts': 100.0,
                                     'pulse': 130.0, 'value': 0.769}])
        # A pulse that has not settled gives no reading.
        for sample in samples:
            if sample['elapsed'] >= 480:
                sample['pulse'] = 140
        self.assertEqual(summarize_workout(samples, chart)['efficiency'], [])

    def test_a_ramp_gives_no_watt_per_beat(self):
        chart = build_chart_plan({'mode': 'power', 'blocks': [
            {'type': 'ramp', 'minutes': 10, 'start_power': 50, 'end_power': 250, 'test': 'ramp'}]})
        samples = [{'elapsed': second, 'pulse': 100 + second // 10, 'power': 50 + second // 3}
                   for second in range(600)]
        self.assertEqual(summarize_workout(samples, chart)['efficiency'], [])


class FitnessTestTests(unittest.TestCase):
    profile = {'birth_year': datetime.date.today().year - 50, 'sex': 'male', 'weight_kg': 80.0}

    def astrand_samples(self, pulse5, pulse6, watts=150, limited=False):
        block = {'start': 180, 'end': 540, 'index': 1}
        samples = [{'elapsed': t, 'pulse': 120, 'power': 75} for t in range(180)]
        for t in range(360):
            pulse = pulse5 if t < 300 else pulse6
            samples.append({'elapsed': 180 + t, 'pulse': pulse, 'power': watts, 'limited': limited})
        return samples, block

    def test_astrand_formula_and_age_factor(self):
        # 150 W = 918 kpm/min; the nomogram gives about 3.4 L/min at pulse 150 for a man.
        self.assertAlmostEqual(astrand_vo2max_litres(150, 150, 'male'), 3.358, places=2)
        # Women have a higher pulse at the same load, so the same pulse means a higher VO2max.
        self.assertGreater(astrand_vo2max_litres(150, 150, 'female'), 3.358)
        self.assertEqual(astrand_age_factor(25), 1.0)
        self.assertEqual(astrand_age_factor(50), 0.75)
        self.assertAlmostEqual(astrand_age_factor(37), 0.854, places=3)
        self.assertAlmostEqual(astrand_age_factor(67), 0.638, places=3)

    def test_valid_astrand_test(self):
        samples, block = self.astrand_samples(148, 150)

        result = analyze_astrand(samples, block, self.profile)

        self.assertTrue(result['valid'], result['problems'])
        self.assertEqual((result['watts'], result['pulse'], result['age_factor']), (150, 149, 0.75))
        expected = astrand_vo2max_litres(150, 149, 'male') * 0.75 * 1000 / 80
        self.assertAlmostEqual(result['vo2max'], round(expected, 1))

    def test_astrand_problems_make_the_test_invalid(self):
        unstable = analyze_astrand(*self.astrand_samples(140, 152), profile=self.profile)
        too_low = analyze_astrand(*self.astrand_samples(105, 106), profile=self.profile)
        limited = analyze_astrand(*self.astrand_samples(148, 150, limited=True), profile=self.profile)
        no_profile = analyze_astrand(*self.astrand_samples(148, 150), profile={})
        short = analyze_astrand(self.astrand_samples(148, 150)[0][:400], {'start': 180, 'end': 540},
                                self.profile)

        self.assertEqual(unstable['problems'][0], {'code': 'astrand_unstable', 'limit': 5})
        self.assertEqual(too_low['problems'][0], {'code': 'astrand_pulse_low', 'limit': 120})
        self.assertEqual(limited['problems'][0], {'code': 'pulse_limited'})
        self.assertEqual(no_profile['problems'][0], {'code': 'astrand_profile_missing'})
        self.assertNotIn('vo2max', no_profile)
        self.assertEqual(short['problems'][0], {'code': 'astrand_incomplete'})
        for result in (unstable, too_low, limited, no_profile, short):
            self.assertFalse(result['valid'])

    def test_astrand_result_is_worked_out_again_for_another_profile(self):
        samples, block = self.astrand_samples(148, 150)
        without = analyze_astrand(samples, block, {})

        moved = rescore_test(without, self.profile)

        self.assertEqual(moved, analyze_astrand(samples, block, self.profile))
        self.assertEqual(rescore_test(moved, {})['problems'], [{'code': 'astrand_profile_missing'}])
        self.assertNotIn('vo2max', rescore_test(moved, {}))
        # A problem that has nothing to do with the profile stays.
        unstable = analyze_astrand(*self.astrand_samples(140, 152), profile={})
        self.assertEqual(rescore_test(unstable, self.profile)['problems'],
                         [{'code': 'astrand_unstable', 'limit': 5}])
        incomplete = {'type': 'astrand', 'valid': False, 'problems': [{'code': 'astrand_incomplete'}]}
        self.assertEqual(rescore_test(incomplete, self.profile), incomplete)

    def history_with(self, sessions):
        directory = tempfile.mkdtemp(prefix='pyergometer-history-test-')
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, 'history.json')
        for session in sessions:
            append_history(path, session)
        return path

    def test_history_sessions_are_deleted_or_moved_by_start_time(self):
        samples, block = self.astrand_samples(148, 150)
        test = analyze_astrand(samples, block, {})
        path = self.history_with([
            {'profile': 'Standard', 'started': '2026-10-28T16:32:45', 'tests': [test]},
            {'profile': 'Standard', 'started': '2026-10-28T17:00:00', 'tests': []},
            {'profile': 'Steen', 'started': '2026-10-28T17:00:00', 'tests': []}])
        steen = dict(self.profile, name='Steen')

        left = change_history(path, 'Standard', ['2026-10-28T16:32:45'], steen)

        self.assertEqual([s['started'] for s in left], ['2026-10-28T17:00:00'])
        moved = [s for s in read_history(path) if s['profile'] == 'Steen']
        self.assertEqual(len(moved), 2)
        self.assertTrue(moved[0]['tests'][0]['valid'])
        # Only the chosen profile's session with that start time is deleted.
        self.assertEqual(change_history(path, 'Standard', ['2026-10-28T17:00:00']), [])
        self.assertEqual([s['profile'] for s in read_history(path)], ['Steen', 'Steen'])
        for started, target in ((['2026-01-01T00:00:00'], None), ([], None),
                                (['2026-10-28T17:00:00'], steen)):
            with self.assertRaises(ValueError):
                change_history(path, 'Steen', started, target)

    def test_history_can_be_changed_from_the_development_page(self):
        directory = tempfile.mkdtemp(prefix='pyergometer-history-api-test-')
        self.addCleanup(shutil.rmtree, directory)
        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        server.history_file = os.path.join(directory, 'history.json')
        server.profiles_file = os.path.join(directory, 'profiles.json')
        server.presets_file = os.path.join(directory, 'workouts.json')
        create_profile(server.profiles_file, server.presets_file, 'Steen')
        append_history(server.history_file, {'profile': 'Standard', 'started': 'a', 'tests': []})
        append_history(server.history_file, {'profile': 'Standard', 'started': 'b', 'tests': []})
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()
        self.addCleanup(stop)

        def post(body):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('POST', '/api/history', body=json.dumps(body), headers={
                'Content-Type': 'application/json',
                'Origin': 'http://127.0.0.1:%d' % server.server_port})
            response = connection.getresponse()
            data = json.loads(response.read().decode('utf-8'))
            connection.close()
            return response.status, data

        status, data = post({'action': 'move', 'profile': 'Standard', 'sessions': ['a'],
                             'target': 'Steen'})
        self.assertEqual((status, [s['started'] for s in data['sessions']]), (200, ['b']))
        status, data = post({'action': 'delete', 'profile': 'Standard', 'sessions': ['b']})
        self.assertEqual((status, data['sessions']), (200, []))
        self.assertEqual(post({'action': 'move', 'profile': 'Steen', 'sessions': ['a'],
                               'target': 'Nobody'})[0], 400)
        self.assertEqual(post({'action': 'rename', 'profile': 'Steen', 'sessions': ['a']})[0], 400)
        self.assertEqual([(s['profile'], s['started']) for s in read_history(server.history_file)],
                         [('Steen', 'a')])

    def test_ramp_test_gives_map_ftp_and_vo2max(self):
        block = {'start': 300, 'end': 2100, 'index': 1}
        samples = []
        for t in range(600):          # 10 minutes of a 20 W/min ramp from 50 W, then exhausted
            power = 50 + t // 3
            samples.append({'elapsed': 300 + t, 'pulse': 110 + t // 10, 'power': power,
                            'target_power': power, 'rpm': 80})
        samples.append({'elapsed': 900, 'pulse': 171, 'power': 250, 'target_power': 250, 'rpm': 20})

        result = analyze_ramp(samples, block, self.profile)

        self.assertTrue(result['valid'], result['problems'])
        self.assertEqual(result['peak_watts'], 249)
        self.assertEqual(result['map_watts'], 240)
        self.assertEqual(result['ftp_watts'], 180)
        self.assertEqual(result['max_pulse'], 171)
        self.assertEqual(result['vo2max'], round(10.8 * 239.5 / 80 + 7, 1))

    def test_short_ramp_gives_no_result(self):
        samples = [{'elapsed': 300 + t, 'pulse': 120, 'power': 60, 'rpm': 80} for t in range(100)]

        result = analyze_ramp(samples, {'start': 300, 'end': 2100, 'index': 1}, self.profile)

        self.assertFalse(result['valid'])
        self.assertEqual(result['problems'][0], {'code': 'ramp_too_short'})

    def test_test_marker_only_on_watt_steps_and_ramps(self):
        chart = build_chart_plan({'mode': 'power', 'blocks': [
            {'type': 'interval', 'minutes': 6, 'power': 100, 'test': 'astrand'}]})
        self.assertEqual(chart['blocks'][0]['test'], 'astrand')
        for plan in ({'mode': 'power', 'blocks': [{'type': 'interval', 'minutes': 6, 'power': 100,
                                                   'test': 'marathon'}]},
                     {'max_pulse': 180, 'blocks': [{'type': 'interval', 'minutes': 6, 'percent': 70,
                                                    'test': 'astrand'}]}):
            with self.assertRaises(ValueError):
                build_chart_plan(plan)


class LanguageTests(unittest.TestCase):
    PLACEHOLDER = re.compile(r'\{(\w+)\}')

    def test_danish_is_there_and_names_itself(self):
        languages = read_languages()

        self.assertEqual(languages['da']['language.name'], 'Dansk')

    def test_every_language_has_the_same_texts_and_placeholders_as_danish(self):
        languages = read_languages()
        danish = languages['da']
        for code, texts in languages.items():
            self.assertEqual(set(texts), set(danish), code)
            for key, text in texts.items():
                if isinstance(text, list):
                    self.assertEqual(len(text), len(danish[key]), (code, key))
                else:
                    self.assertEqual(set(self.PLACEHOLDER.findall(text)),
                                     set(self.PLACEHOLDER.findall(danish[key])), (code, key))

    def test_every_message_code_from_the_server_has_a_text(self):
        source = open(os.path.join(os.path.dirname(__file__), 'workout.py'), encoding='utf-8').read()
        danish = read_languages()['da']
        problems = re.findall(r"problems'\]\.append\(\{'code': '(\w+)'", source)
        codes = re.findall(r"'code': '(\w+)'", source) + re.findall(r"'code': '(\w+)' if", source)
        codes += re.findall(r"else '(\w+)',", source)

        self.assertGreaterEqual(len(problems), 8)
        for code in problems:
            self.assertIn('problem.' + code, danish)
        for code in set(codes) - set(problems):
            self.assertIn('server.' + code, danish)

    def test_shared_template_names_cannot_be_used_for_saved_workouts(self):
        self.assertIn('Test · Åstrand (submaksimal)', builtin_template_names())
        directory = tempfile.mkdtemp()
        try:
            with self.assertRaises(ValueError):
                save_preset(os.path.join(directory, 'presets.json'), 'test · åstrand (submaksimal)',
                            {'blocks': [{'type': 'interval', 'minutes': 1, 'percent': 70}]})
        finally:
            shutil.rmtree(directory)

    def test_language_script_carries_the_texts(self):
        server = WorkoutServer(('127.0.0.1', 0), simulate=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('GET', '/i18n.js')
            response = connection.getresponse()
            script = response.read().decode('utf-8')
            connection.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(response.status, 200)
        self.assertIn('javascript', response.getheader('Content-Type'))
        self.assertNotIn('@@LANGUAGES@@', script)
        self.assertIn('"development.title": "Min udvikling"', script)


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
