import datetime
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser
import xml.etree.ElementTree as ET
from bleheartrate import HeartRateMonitor, scan_heart_rate_devices
from liveplan import ramp_value, session_steps
from devices import DEFAULT_DEVICE_TYPE, device_type, device_type_ids, public_device_types
try:
    from urllib.parse import parse_qs, urlparse
except ImportError:
    from urlparse import parse_qs, urlparse

try:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn
except ImportError:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn


MAX_REQUEST_SIZE = 65536
MAX_WORKOUT_SECONDS = 12 * 60 * 60
WORKOUT_MODES = ('pulse', 'power')
MIN_POWER = 25
MAX_POWER = 1000
# In watt-controlled workouts the load is reduced above this share of maximum pulse.
PULSE_LIMIT_PERCENT = 95
# Seconds a start waits for the first watch pulse: finding and connecting the
# watch takes up to about 15 seconds on a Raspberry Pi.
WATCH_START_TIMEOUT = 30
STARTING_MESSAGE = ('Træningen er ved at starte og venter på pulsen fra uret '
                    '(op til %d sekunder).' % WATCH_START_TIMEOUT)
BUILTIN_TEMPLATE_NAMES = ('Puls · HIIT 30/30 (13 gentagelser)', 'Watt · VO₂ Max 5 × 5',
                          'HIIT 30/30 (13 gentagelser)')

try:
    integer_types = (int, long)
    text_types = (str, unicode)
except NameError:
    integer_types = (int,)
    text_types = (str,)


def _utcnow():
    if hasattr(datetime, 'timezone'):
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return datetime.datetime.utcnow()


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, integer_types):
        raise ValueError('%s must be a whole number' % name)
    if value < minimum or value > maximum:
        raise ValueError('%s must be between %d and %d' % (name, minimum, maximum))
    return value


def _duration_seconds(value, name):
    """Block durations are given in minutes, in half-minute steps from 30 seconds."""
    if isinstance(value, bool) or not isinstance(value, integer_types + (float,)):
        raise ValueError('%s must be a number of minutes' % name)
    half_minutes = value * 2
    if half_minutes != int(half_minutes):
        raise ValueError('%s must be in half-minute steps' % name)
    if value < 0.5 or value > 720:
        raise ValueError('%s must be between 0.5 and 720 minutes' % name)
    return int(half_minutes) * 30


def _maximum_pulse(plan):
    source = plan.get('max_pulse_source', 'manual')
    if source == 'age':
        age = _integer(plan.get('age'), 'Age', 1, 120)
        return (2080 - 7 * age + 5) // 10
    if source != 'manual':
        raise ValueError('Choose a maximum-pulse source')

    maximum_pulse = _integer(plan.get('max_pulse'), 'Maximum pulse', 100, 220)
    if maximum_pulse % 10:
        raise ValueError('Maximum pulse must be selected in 10-bpm increments')
    return maximum_pulse


def normalize_preset_plan(plan):
    """Keep only the mode and blocks; maximum pulse belongs to the user profile."""
    if not isinstance(plan, dict):
        raise ValueError('Workout data must be an object')
    mode = plan.get('mode', 'pulse')
    # Validate the blocks with a placeholder maximum; pulse targets are stored as percentages.
    build_chart_plan({'mode': mode, 'max_pulse_source': 'manual', 'max_pulse': 200,
                      'blocks': plan.get('blocks')})
    normalized = {'blocks': [dict(block) for block in plan['blocks']]}
    if mode != 'pulse':
        normalized['mode'] = mode
    return normalized


def normalize_profile_max_pulse(max_pulse):
    """Validate a profile's maximum pulse: a fixed value or one derived from birth year."""
    if max_pulse is None:
        return None
    if not isinstance(max_pulse, dict):
        raise ValueError('Maximum pulse must be an object')
    source = max_pulse.get('source')
    if source == 'manual':
        value = _integer(max_pulse.get('value'), 'Maximum pulse', 100, 220)
        if value % 10:
            raise ValueError('Maximum pulse must be selected in 10-bpm increments')
        return {'source': 'manual', 'value': value}
    if source == 'birth_year':
        this_year = _utcnow().year
        birth_year = _integer(max_pulse.get('birth_year'), 'Birth year',
                              this_year - 120, this_year - 1)
        return {'source': 'birth_year', 'birth_year': birth_year}
    raise ValueError('Choose a maximum-pulse source')


def read_presets(path):
    if not os.path.exists(path):
        return []

    with open(path, 'rb') as preset_file:
        data = json.loads(preset_file.read().decode('utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('presets'), list):
        raise ValueError('Saved workout presets file has an invalid format')

    presets = []
    for preset in data['presets']:
        if not isinstance(preset, dict) or not isinstance(preset.get('name'), text_types):
            raise ValueError('Saved workout preset has an invalid format')
        presets.append({
            'name': preset['name'],
            'plan': normalize_preset_plan(preset.get('plan'))
        })
    return presets


def save_preset(path, name, plan):
    if not isinstance(name, text_types):
        raise ValueError('Preset name must be text')
    name = name.strip()
    if not name or len(name) > 80:
        raise ValueError('Preset name must contain between 1 and 80 characters')
    if name.lower() in (template_name.lower() for template_name in BUILTIN_TEMPLATE_NAMES):
        raise ValueError('Choose a new name when saving a shared workout template')

    normalized_plan = normalize_preset_plan(plan)
    presets = read_presets(path)
    replaced = False
    for preset in presets:
        if preset['name'].lower() == name.lower():
            preset['name'] = name
            preset['plan'] = normalized_plan
            replaced = True
            break
    if not replaced:
        presets.append({'name': name, 'plan': normalized_plan})

    directory = os.path.dirname(os.path.abspath(path))
    temporary = tempfile.NamedTemporaryFile(mode='wb', prefix='workouts-', suffix='.tmp',
                                            dir=directory, delete=False)
    try:
        content = json.dumps({'presets': presets}, ensure_ascii=False, indent=2)
        if not isinstance(content, bytes):
            content = content.encode('utf-8')
        temporary.write(content)
        temporary.flush()
        temporary.close()
        if hasattr(os, 'replace'):
            os.replace(temporary.name, path)
        else:
            if os.path.exists(path):
                os.remove(path)
            os.rename(temporary.name, path)
    except Exception:
        temporary.close()
        if os.path.exists(temporary.name):
            os.remove(temporary.name)
        raise
    return presets


def _profile_name(name):
    if not isinstance(name, text_types):
        raise ValueError('Profile name must be text')
    name = name.strip()
    if not name or len(name) > 60:
        raise ValueError('Profile name must contain between 1 and 60 characters')
    return name


def read_profiles(path, legacy_presets_path=None):
    if not os.path.exists(path):
        presets = read_presets(legacy_presets_path) if legacy_presets_path else []
        return [{'name': 'Standard', 'max_pulse': None, 'watch': None, 'presets': presets}]

    with open(path, 'rb') as profile_file:
        data = json.loads(profile_file.read().decode('utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('profiles'), list):
        raise ValueError('Saved user profiles file has an invalid format')

    profiles = []
    names = set()
    for profile in data['profiles']:
        if not isinstance(profile, dict):
            raise ValueError('Saved user profile has an invalid format')
        name = _profile_name(profile.get('name'))
        if name.lower() in names:
            raise ValueError('Saved user profiles contain duplicate names')
        names.add(name.lower())
        watch = profile.get('watch')
        if watch is not None:
            if (not isinstance(watch, dict) or
                    not isinstance(watch.get('name'), text_types) or
                    not isinstance(watch.get('address'), text_types)):
                raise ValueError('Saved smartwatch data has an invalid format')
            watch = {'name': watch['name'], 'address': watch['address']}
        presets = profile.get('presets')
        if not isinstance(presets, list):
            raise ValueError('Saved profile workouts have an invalid format')
        normalized_presets = []
        for preset in presets:
            if not isinstance(preset, dict) or not isinstance(preset.get('name'), text_types):
                raise ValueError('Saved workout preset has an invalid format')
            normalized_presets.append({
                'name': preset['name'],
                'plan': normalize_preset_plan(preset.get('plan'))
            })
        profiles.append({
            'name': name,
            'max_pulse': normalize_profile_max_pulse(profile.get('max_pulse')),
            'watch': watch,
            'presets': normalized_presets
        })
    return profiles


def write_profiles(path, profiles):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = tempfile.NamedTemporaryFile(
        mode='wb', prefix='profiles-', suffix='.tmp', dir=directory, delete=False)
    try:
        content = json.dumps({'profiles': profiles}, ensure_ascii=False, indent=2)
        if not isinstance(content, bytes):
            content = content.encode('utf-8')
        temporary.write(content)
        temporary.flush()
        temporary.close()
        if hasattr(os, 'replace'):
            os.replace(temporary.name, path)
        else:
            if os.path.exists(path):
                os.remove(path)
            os.rename(temporary.name, path)
    except Exception:
        temporary.close()
        if os.path.exists(temporary.name):
            os.remove(temporary.name)
        raise


def normalize_bike_settings(settings):
    """Validate the bike type and serial port chosen on the page."""
    if not isinstance(settings, dict):
        raise ValueError('Bike settings must be an object')
    type_id = settings.get('device_type')
    if type_id not in device_type_ids():
        raise ValueError('Unknown ergometer type: %s' % type_id)
    device = settings.get('device', '')
    if not isinstance(device, text_types) or len(device) > 128:
        raise ValueError('Serial device must be text of at most 128 characters')
    return {'device_type': type_id, 'device': device.strip()}


def read_bike_settings(path):
    """Return the saved bike settings, or None when nothing usable is saved."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as settings_file:
            return normalize_bike_settings(json.loads(settings_file.read().decode('utf-8')))
    except (IOError, ValueError, TypeError) as error:
        sys.stderr.write('Ignoring saved bike settings: %s\n' % error)
        return None


def write_bike_settings(path, settings):
    settings = normalize_bike_settings(settings)
    directory = os.path.dirname(os.path.abspath(path))
    temporary = tempfile.NamedTemporaryFile(
        mode='wb', prefix='bike-settings-', suffix='.tmp', dir=directory, delete=False)
    try:
        temporary.write(json.dumps(settings, indent=2).encode('utf-8'))
        temporary.close()
        os.replace(temporary.name, path)
    except Exception:
        temporary.close()
        if os.path.exists(temporary.name):
            os.remove(temporary.name)
        raise
    return settings


def create_profile(path, legacy_presets_path, name):
    name = _profile_name(name)
    profiles = read_profiles(path, legacy_presets_path)
    if any(profile['name'].lower() == name.lower() for profile in profiles):
        raise ValueError('A profile with this name already exists')
    profiles.append({'name': name, 'max_pulse': None, 'watch': None, 'presets': []})
    write_profiles(path, profiles)
    return profiles


def update_profile_watch(path, legacy_presets_path, profile_name, watch):
    """Save the profile's smartwatch, or forget it when watch is None."""
    profile_name = _profile_name(profile_name)
    if watch is not None:
        if not isinstance(watch, dict):
            raise ValueError('Smartwatch data must be an object')
        name = watch.get('name')
        address = watch.get('address')
        if (not isinstance(name, text_types) or not name.strip() or len(name) > 120 or
                not isinstance(address, text_types) or not address.strip() or
                len(address) > 128):
            raise ValueError('Choose a valid smartwatch')
        watch = {'name': name.strip(), 'address': address.strip()}
    profiles = read_profiles(path, legacy_presets_path)
    for profile in profiles:
        if profile['name'] == profile_name:
            profile['watch'] = watch
            write_profiles(path, profiles)
            return profiles
    raise ValueError('User profile was not found')


def update_profile_max_pulse(path, legacy_presets_path, profile_name, max_pulse):
    profile_name = _profile_name(profile_name)
    if max_pulse is None:
        raise ValueError('Choose a maximum pulse')
    max_pulse = normalize_profile_max_pulse(max_pulse)
    profiles = read_profiles(path, legacy_presets_path)
    for profile in profiles:
        if profile['name'] == profile_name:
            profile['max_pulse'] = max_pulse
            write_profiles(path, profiles)
            return profiles
    raise ValueError('User profile was not found')


def save_profile_preset(path, legacy_presets_path, profile_name, name, plan):
    profile_name = _profile_name(profile_name)
    if not isinstance(name, text_types):
        raise ValueError('Preset name must be text')
    name = name.strip()
    if not name or len(name) > 80:
        raise ValueError('Preset name must contain between 1 and 80 characters')
    if name.lower() in (template_name.lower() for template_name in BUILTIN_TEMPLATE_NAMES):
        raise ValueError('Choose a new name when saving a shared workout template')
    normalized_plan = normalize_preset_plan(plan)
    profiles = read_profiles(path, legacy_presets_path)
    for profile in profiles:
        if profile['name'] == profile_name:
            for preset in profile['presets']:
                if preset['name'].lower() == name.lower():
                    preset['name'] = name
                    preset['plan'] = normalized_plan
                    break
            else:
                profile['presets'].append({'name': name, 'plan': normalized_plan})
            write_profiles(path, profiles)
            return profile['presets']
    raise ValueError('User profile was not found')


def _target_pulse(maximum_pulse, percent):
    return (maximum_pulse * percent + 50) // 100


def _power(value, name):
    return _integer(value, name, MIN_POWER, MAX_POWER)


def _optional_maximum_pulse(plan):
    try:
        return _maximum_pulse(plan)
    except ValueError:
        return None


def build_chart_plan(plan):
    """Validate a workout and expand it into chart blocks with absolute times.

    A workout is either pulse-controlled (mode 'pulse', targets in percent of
    maximum pulse) or watt-controlled (mode 'power', loads in watts). Pauses at
    a fixed load are allowed in both; ramps change the target linearly.
    """
    if not isinstance(plan, dict):
        raise ValueError('Workout data must be an object')
    mode = plan.get('mode', 'pulse')
    if mode not in WORKOUT_MODES:
        raise ValueError('Workout mode must be pulse or power')
    maximum_pulse = _maximum_pulse(plan) if mode == 'pulse' else _optional_maximum_pulse(plan)
    blocks = plan.get('blocks')
    if not isinstance(blocks, list) or not blocks:
        raise ValueError('Add at least one interval or break')

    chart_blocks = []
    elapsed = 0
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ValueError('Block %d is invalid' % (index + 1))
        label = 'Block %d' % (index + 1)
        block_type = block.get('type')
        entries = []
        if block_type == 'interval':
            duration = _duration_seconds(block.get('minutes'), '%s duration' % label)
            if mode == 'pulse':
                percent = _integer(block.get('percent'), '%s target' % label, 1, 100)
                entries.append(({'type': 'interval', 'percent': percent,
                                 'pulse': _target_pulse(maximum_pulse, percent)}, duration))
            else:
                entries.append(({'type': 'power',
                                 'power': _power(block.get('power'), '%s load' % label)},
                                duration))
        elif block_type == 'break':
            duration = _duration_seconds(block.get('minutes'), '%s duration' % label)
            power = _integer(block.get('power'), '%s load' % label, 25, 400)
            if power % 5:
                raise ValueError('%s load must be in 5-watt increments' % label)
            entries.append(({'type': 'break', 'power': power}, duration))
        elif block_type == 'ramp':
            duration = _duration_seconds(block.get('minutes'), '%s duration' % label)
            if mode == 'pulse':
                first = _integer(block.get('start_percent'), '%s start' % label, 1, 100)
                last = _integer(block.get('end_percent'), '%s end' % label, 1, 100)
                entries.append(({'type': 'ramp', 'kind': 'pulse',
                                 'start_percent': first, 'end_percent': last,
                                 'start_value': _target_pulse(maximum_pulse, first),
                                 'end_value': _target_pulse(maximum_pulse, last)}, duration))
            else:
                entries.append(({'type': 'ramp', 'kind': 'power',
                                 'start_value': _power(block.get('start_power'), '%s start' % label),
                                 'end_value': _power(block.get('end_power'), '%s end' % label)},
                                duration))
        elif block_type == 'repeat':
            count = _integer(block.get('count'), 'Repeat count %d' % (index + 1), 1, 100)
            phases = []
            for phase in ('work', 'recovery'):
                seconds = _integer(block.get(phase + '_seconds'),
                                   '%s %s duration' % (label, phase), 1, 3600)
                if mode == 'pulse':
                    percent = _integer(block.get(phase + '_percent'),
                                       '%s %s target' % (label, phase), 1, 100)
                    entry = {'type': 'interval', 'percent': percent,
                             'pulse': _target_pulse(maximum_pulse, percent)}
                else:
                    entry = {'type': 'power',
                             'power': _power(block.get(phase + '_power'),
                                             '%s %s load' % (label, phase))}
                entry.update({'draggable': False, 'repeat_phase': phase})
                phases.append((entry, seconds))
            for unused in range(count):
                entries.extend((dict(entry), seconds) for entry, seconds in phases)
        else:
            raise ValueError('%s must be an interval, break, ramp or repeat' % label)

        for entry, duration in entries:
            entry.update({'index': index, 'start': elapsed, 'end': elapsed + duration})
            chart_blocks.append(entry)
            elapsed += duration
        if elapsed > MAX_WORKOUT_SECONDS:
            raise ValueError('Total workout duration cannot exceed 12 hours')

    chart_plan = {'mode': mode, 'max_pulse': maximum_pulse, 'duration': elapsed,
                  'blocks': chart_blocks}
    # The workout name is only shown above the chart; it does not affect control.
    name = plan.get('name')
    if isinstance(name, text_types) and name.strip():
        chart_plan['name'] = name.strip()[:100]
    return chart_plan


def pulse_limit(chart_plan, enabled=True):
    """The pulse at which a watt-controlled workout lowers its load, or None."""
    if not enabled or chart_plan['mode'] != 'power' or not chart_plan['max_pulse']:
        return None
    return chart_plan['max_pulse'] * PULSE_LIMIT_PERCENT // 100


def build_session_xml(plan):
    """Session XML for the controller, built from the same per-second steps as live updates."""
    steps = session_steps(build_chart_plan(plan))
    events = []
    for step in steps:
        action, value = ('power', step['power']) if step['power'] > 0 else ('pulse', step['pulse'])
        if events and events[-1][0] == action and events[-1][1] == value:
            events[-1][2] += 1
        else:
            events.append([action, value, 1])

    root = ET.Element('program', name='Custom workout', duration='%ds' % len(steps))
    timeline = ET.SubElement(root, 'timeEvents')
    for index, (action, value, unused) in enumerate(events):
        event_time = '0' if index == 0 else '+%ds' % events[index - 1][2]
        event = ET.SubElement(timeline, 'timeEvent', time=event_time)
        ET.SubElement(event, 'action', type=action, value=str(value))
    return ET.tostring(root, encoding='utf-8')


def _sampled_points(points, limit=600):
    if len(points) <= limit:
        return points
    stride = (len(points) - 1 + limit - 2) // (limit - 1)
    sampled = points[::stride]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


LOOPBACK_HOSTS = ('127.0.0.1', 'localhost')
QUIET_PATHS = ('/api/status', '/api/chart')


def local_network_addresses():
    """Return the hostnames and IPv4 addresses this computer is reachable as."""
    hosts = set()
    name = socket.gethostname().lower()
    if name:
        hosts.update((name, name + '.local'))
        try:
            for info in socket.getaddrinfo(name, None, socket.AF_INET):
                hosts.add(info[4][0])
        except socket.error:
            pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a UDP socket sends nothing; it only selects the outgoing interface.
        probe.connect(('10.255.255.255', 1))
        hosts.add(probe.getsockname()[0])
    except socket.error:
        pass
    finally:
        probe.close()
    hosts.discard('0.0.0.0')
    return hosts


class WorkoutServer(ThreadingMixIn, HTTPServer):
    # Each request gets its own thread, so a phone polling the page is not kept
    # waiting while another device scans for watches. Requests that read or change
    # the server state still take turns through ``lock``.
    daemon_threads = True

    def __init__(self, address, device=None, simulate=False, device_type=None,
                 bike_settings_file=None):
        HTTPServer.__init__(self, address, WorkoutRequestHandler)
        self.lock = threading.RLock()
        # True while a start waits for the first watch pulse without holding the lock.
        self.starting = False
        self.bind_host = address[0]
        self.bike_settings_file = bike_settings_file or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'bike_settings.json')
        # Command-line options win; otherwise use what was last chosen on the page.
        saved = read_bike_settings(self.bike_settings_file) or {}
        self.device = device or saved.get('device', '')
        self.simulate = bool(simulate)
        self.device_type = 'simulator' if simulate else (
            device_type or saved.get('device_type') or DEFAULT_DEVICE_TYPE)
        self.process = None
        self.stop_requested = False
        self.session_file = None
        self.log_json = None
        self.last_exit_code = None
        self.last_error = None
        self.presets_file = os.path.join(os.path.dirname(__file__), 'workouts.json')
        self.profiles_file = os.path.join(os.path.dirname(__file__), 'user_profiles.json')
        self.chart_plan = None
        self.chart_live_path = None
        self.chart_control_path = None
        self.heart_rate_monitor = None
        self.heart_rate_path = None
        self.chart_file_position = 0
        self.chart_points = []
        self.chart_limited = False
        self.demo_started_at = None
        self.demo_elapsed = 0
        self.demo_pulse = 82
        self.demo_block_index = 0
        self.demo_points = []

    def start_demo(self, plan):
        self.chart_plan = build_chart_plan(plan)
        self.chart_live_path = None
        self.chart_file_position = 0
        self.chart_points = []
        self.demo_started_at = _utcnow()
        self.demo_elapsed = 0
        self.demo_pulse = 82
        self.demo_block_index = 0
        self.demo_points = [{'elapsed': 0, 'pulse': self.demo_pulse}]

    def reset_chart(self):
        self.chart_limited = False
        self.chart_plan = None
        self.chart_live_path = None
        self.chart_control_path = None
        self.chart_file_position = 0
        self.chart_points = []
        self.demo_started_at = None
        self.demo_elapsed = 0
        self.demo_points = []

    def advance_demo(self):
        if self.demo_started_at is None or self.chart_plan is None:
            return

        elapsed = min(
            self.chart_plan['duration'],
            int((_utcnow() - self.demo_started_at).total_seconds()))
        while self.demo_elapsed < elapsed:
            self.demo_elapsed += 1
            while (self.demo_block_index + 1 < len(self.chart_plan['blocks']) and
                   self.demo_elapsed >= self.chart_plan['blocks'][self.demo_block_index]['end']):
                self.demo_block_index += 1

            block = self.chart_plan['blocks'][self.demo_block_index]
            maximum_pulse = self.chart_plan.get('max_pulse') or 190
            if block['type'] == 'interval':
                target = block['pulse']
            elif block['type'] == 'ramp':
                value = ramp_value(block['start_value'], block['end_value'],
                                   block['end'] - block['start'],
                                   self.demo_elapsed - block['start'])
                target = value if block['kind'] == 'pulse' else 70 + value * 0.3
            elif block['type'] == 'power':
                # A rough pulse response to the load, for the demo only.
                target = 70 + block['power'] * 0.3
            else:
                target = min(115, int(maximum_pulse * 0.65))

            drift = (target - self.demo_pulse) * 0.025
            variation = 1.2 * math.sin(self.demo_elapsed / 9.0)
            self.demo_pulse = max(
                55, min(maximum_pulse, int(round(self.demo_pulse + drift + variation))))
            self.demo_points.append({
                'elapsed': self.demo_elapsed,
                'pulse': self.demo_pulse
                })

    def allowed_origin_hosts(self):
        hosts = set(LOOPBACK_HOSTS)
        if self.bind_host in LOOPBACK_HOSTS:
            return hosts
        if self.bind_host not in ('', '0.0.0.0'):
            hosts.add(self.bind_host)
        # Looked up per request so a Raspberry Pi that gets its Wi-Fi address after start-up works.
        hosts.update(local_network_addresses())
        return hosts

    def refresh_process(self):
        if self.process is None:
            return

        exit_code = self.process.poll()
        if exit_code is None:
            return

        self.process = None
        self.last_exit_code = exit_code
        self.stop_requested = False
        if self.session_file and os.path.exists(self.session_file):
            os.remove(self.session_file)
        self.session_file = None
        if self.heart_rate_monitor:
            self.heart_rate_monitor.stop()
            self.heart_rate_monitor = None
        if self.heart_rate_path and os.path.exists(self.heart_rate_path):
            os.remove(self.heart_rate_path)
        heart_rate_status_path = (self.heart_rate_path + '.status') \
            if self.heart_rate_path else None
        if heart_rate_status_path and os.path.exists(heart_rate_status_path):
            os.remove(heart_rate_status_path)
        self.heart_rate_path = None
        if self.chart_control_path and os.path.exists(self.chart_control_path):
            os.remove(self.chart_control_path)
        self.chart_control_path = None

        if exit_code == 0 and self.log_json and not os.path.exists(self.log_json):
            self.last_error = 'Workout finished, but its JSON log was not created'


class WorkoutRequestHandler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, data):
        self._send(status, json.dumps(data), 'application/json; charset=utf-8')

    def _chart_data(self):
        self.server.refresh_process()
        if self.server.demo_started_at is not None:
            self.server.advance_demo()
            elapsed = self.server.demo_elapsed
            points = self.server.demo_points
            mode = 'demo'
            active = elapsed < self.server.chart_plan['duration']
        elif self.server.chart_live_path:
            points = self.server.chart_points
            elapsed = 0
            try:
                with open(self.server.chart_live_path, 'rb') as live_file:
                    live_file.seek(self.server.chart_file_position)
                    chunk = live_file.read()
                complete_chunk = chunk
                if chunk and not chunk.endswith(b'\n'):
                    newline = chunk.rfind(b'\n')
                    complete_chunk = chunk[:newline + 1] if newline >= 0 else b''
                for line in complete_chunk.splitlines():
                    if line.strip():
                        sample = json.loads(line.decode('utf-8'))
                        self.server.chart_limited = sample.get('limited') is True
                        points.append({
                            'elapsed': int(sample['elapsed']),
                            'pulse': int(sample['pulse'])
                            })
                self.server.chart_file_position += len(complete_chunk)
                if points:
                    elapsed = points[-1]['elapsed']
            except (IOError, ValueError, KeyError, TypeError) as error:
                self._send_json(500, {'error': 'Could not read live pulse data: %s' % error})
                return
            mode = 'real'
            active = self.server.process is not None
        else:
            points = []
            elapsed = 0
            mode = 'preview'
            active = False

        self._send_json(200, {
            'plan': self.server.chart_plan,
            'elapsed': elapsed,
            'points': _sampled_points(points),
            'mode': mode,
            'active': active,
            'limited': mode == 'real' and active and self.server.chart_limited
            })

    def do_GET(self):
        with self.server.lock:
            self._handle_get()

    def do_POST(self):
        if urlparse(self.path).path == '/api/heart-rate/scan':
            # A scan takes several seconds and touches no server state.
            self._handle_post()
            return
        with self.server.lock:
            self._handle_post()

    def _handle_get(self):
        request_url = urlparse(self.path)
        request_path = request_url.path
        if request_path == '/':
            page_path = os.path.join(os.path.dirname(__file__), 'workout.html')
            with open(page_path, 'rb') as page_file:
                page = page_file.read()
            page = page.replace(b'@@INITIAL_DEVICE@@',
                                json.dumps(self.server.device).encode('utf-8'))
            page = page.replace(b'@@INITIAL_DEVICE_TYPE@@',
                                json.dumps(self.server.device_type).encode('utf-8'))
            page = page.replace(b'@@DEVICE_TYPES@@',
                                json.dumps(public_device_types()).encode('utf-8'))
            self._send(200, page, 'text/html; charset=utf-8')
            return

        if request_path == '/api/status':
            self.server.refresh_process()
            running = self.server.process is not None
            heart_rate = self.server.heart_rate_monitor.status() \
                if self.server.heart_rate_monitor else None
            self._send_json(200, {
                'running': running,
                'starting': self.server.starting,
                'stopping': self.server.stop_requested,
                'can_stop': True,
                'exit_code': self.server.last_exit_code,
                'error': self.server.last_error,
                'log': os.path.basename(self.server.log_json) if self.server.log_json else None,
                'heart_rate': heart_rate
            })
            return

        if request_path == '/api/chart':
            self._chart_data()
            return

        if request_path == '/api/profiles':
            try:
                profiles = read_profiles(self.server.profiles_file, self.server.presets_file)
            except (IOError, ValueError, TypeError) as error:
                self._send_json(500, {'error': 'Could not read user profiles: %s' % error})
                return
            self._send_json(200, {'profiles': profiles})
            return

        if request_path == '/api/presets':
            profile_values = parse_qs(request_url.query).get('profile', ['Standard'])
            if len(profile_values) != 1:
                self._send_json(400, {'error': 'Choose one user profile'})
                return
            try:
                profile_name = _profile_name(profile_values[0])
                profiles = read_profiles(self.server.profiles_file, self.server.presets_file)
                profile = next(
                    (item for item in profiles if item['name'] == profile_name), None)
                if profile is None:
                    raise ValueError('User profile was not found')
            except (IOError, ValueError, TypeError) as error:
                self._send_json(500, {'error': 'Could not read saved workouts: %s' % error})
                return
            self._send_json(200, {'presets': profile['presets']})
            return

        self._send_json(404, {'error': 'Not found'})

    def _handle_post(self):
        request_path = urlparse(self.path).path
        if request_path not in ('/api/start', '/api/presets', '/api/profiles',
                                '/api/chart/reset',
                                '/api/demo', '/api/update', '/api/stop',
                                '/api/heart-rate/scan', '/api/bike-settings'):
            self._send_json(404, {'error': 'Not found'})
            return

        origin = urlparse(self.headers.get('Origin') or '')
        try:
            origin_port = origin.port
        except ValueError:
            origin_port = None
        if (origin.scheme != 'http' or origin_port != self.server.server_port or
                (origin.hostname or '').lower() not in self.server.allowed_origin_hosts()):
            self._discard_body()
            self._send_json(403, {'error': 'Workout requests must come from the workout page'})
            return
        content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if content_type != 'application/json':
            self._discard_body()
            self._send_json(415, {'error': 'Workout data must use application/json'})
            return

        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self._send_json(400, {'error': 'Invalid request size'})
            return
        if length < 1 or length > MAX_REQUEST_SIZE:
            self._send_json(400, {'error': 'Workout request is empty or too large'})
            return

        try:
            request = json.loads(self.rfile.read(length))
        except (ValueError, TypeError) as error:
            self._send_json(400, {'error': str(error)})
            return

        if request_path == '/api/stop':
            self.server.refresh_process()
            if self.server.process is None:
                self._send_json(409, {'error': 'No workout is currently running'})
                return
            if self.server.stop_requested:
                self._send_json(200, {'stopping': True})
                return
            if not self.server.chart_control_path:
                self._send_json(500, {'error': 'Live workout control is not available'})
                return
            try:
                with open(self.server.chart_control_path, 'ab') as control_file:
                    control_file.write(b'{"stop":true}\n')
                    control_file.flush()
            except (IOError, OSError) as error:
                self._send_json(500, {'error': 'Could not stop workout: %s' % error})
                return
            self.server.stop_requested = True
            self._send_json(200, {'stopping': True})
            return

        if request_path == '/api/chart/reset':
            self.server.refresh_process()
            if self.server.process is not None:
                self._send_json(409, {'error': 'Cannot reset the chart during a workout'})
                return
            self.server.reset_chart()
            self._send_json(200, {'reset': True})
            return

        if request_path == '/api/heart-rate/scan':
            try:
                devices = scan_heart_rate_devices()
            except (RuntimeError, OSError, ValueError) as error:
                self._send_json(500, {'error': 'Could not scan for Bluetooth devices: %s' % error})
                return
            self._send_json(200, {'devices': devices})
            return

        if request_path == '/api/bike-settings':
            try:
                settings = write_bike_settings(self.server.bike_settings_file, request)
            except (IOError, OSError, ValueError, TypeError) as error:
                self._send_json(400, {'error': 'Could not save bike settings: %s' % error})
                return
            self.server.device_type = settings['device_type']
            self.server.device = settings['device']
            self._send_json(200, {'settings': settings})
            return

        if request_path == '/api/profiles':
            if not isinstance(request, dict):
                self._send_json(400, {'error': 'Profile data must be an object'})
                return
            try:
                if request.get('action') == 'create':
                    profiles = create_profile(
                        self.server.profiles_file, self.server.presets_file,
                        request.get('name'))
                elif request.get('action') == 'save-max-pulse':
                    profiles = update_profile_max_pulse(
                        self.server.profiles_file, self.server.presets_file,
                        request.get('profile'), request.get('max_pulse'))
                elif request.get('action') == 'save-watch':
                    if request.get('watch') is None:
                        raise ValueError('Choose a valid smartwatch')
                    profiles = update_profile_watch(
                        self.server.profiles_file, self.server.presets_file,
                        request.get('profile'), request.get('watch'))
                elif request.get('action') == 'remove-watch':
                    profiles = update_profile_watch(
                        self.server.profiles_file, self.server.presets_file,
                        request.get('profile'), None)
                else:
                    raise ValueError('Choose a supported profile action')
            except (IOError, ValueError, TypeError, OSError) as error:
                self._send_json(400, {'error': 'Could not save user profile: %s' % error})
                return
            self._send_json(200, {'profiles': profiles})
            return

        if request_path == '/api/presets':
            if not isinstance(request, dict):
                self._send_json(400, {'error': 'Preset data must be an object'})
                return
            try:
                presets = save_profile_preset(
                    self.server.profiles_file, self.server.presets_file,
                    request.get('profile', 'Standard'), request.get('name'),
                    request.get('plan'))
            except (IOError, ValueError, TypeError, OSError) as error:
                self._send_json(400, {'error': 'Could not save workout: %s' % error})
                return
            self._send_json(200, {'presets': presets})
            return

        if request_path == '/api/demo':
            try:
                build_session_xml(request)
            except (ValueError, TypeError) as error:
                self._send_json(400, {'error': str(error)})
                return
            self.server.refresh_process()
            if self.server.starting:
                self._send_json(409, {'error': STARTING_MESSAGE})
                return
            if self.server.process is not None:
                self._send_json(409, {'error': 'A workout is already running'})
                return
            self.server.start_demo(request)
            self._send_json(200, {'demo': True})
            return

        if request_path == '/api/update':
            try:
                build_session_xml(request)
            except (ValueError, TypeError) as error:
                self._send_json(400, {'error': str(error)})
                return
            self.server.refresh_process()
            if self.server.process is None:
                self._send_json(409, {'error': 'No workout is currently running'})
                return
            if not self.server.chart_control_path:
                self._send_json(500, {'error': 'Live workout control is not available'})
                return
            chart_plan = build_chart_plan(request)
            try:
                update = json.dumps(chart_plan)
                if not isinstance(update, bytes):
                    update = update.encode('utf-8')
                with open(self.server.chart_control_path, 'ab') as control_file:
                    control_file.write(update + b'\n')
                    control_file.flush()
            except (IOError, OSError) as error:
                self._send_json(500, {'error': 'Could not send live workout update: %s' % error})
                return
            self.server.chart_plan = chart_plan
            self._send_json(200, {'updated': True})
            return

        try:
            session_xml = build_session_xml(request)
        except (ValueError, TypeError) as error:
            self._send_json(400, {'error': str(error)})
            return

        self.server.refresh_process()
        if self.server.starting:
            self._send_json(409, {'error': STARTING_MESSAGE})
            return
        if self.server.process is not None:
            self._send_json(409, {'error': 'A workout is already running'})
            return

        simulate = request.get('simulate')
        if simulate is not None and not isinstance(simulate, bool):
            self._send_json(400, {'error': 'Simulation setting must be true or false'})
            return
        type_id = request.get('device_type')
        if type_id is None:
            # Older pages only send the simulation checkbox.
            if simulate:
                type_id = 'simulator'
            elif simulate is False and self.server.device_type == 'simulator':
                type_id = DEFAULT_DEVICE_TYPE
            else:
                type_id = self.server.device_type
        try:
            selected_type = device_type(type_id)
        except ValueError as error:
            self._send_json(400, {'error': str(error)})
            return
        device = request.get('device', self.server.device)
        if not isinstance(device, text_types):
            self._send_json(400, {'error': 'Serial device must be text'})
            return
        if selected_type['needs_port'] and not device.strip():
            self._send_json(400, {'error': 'Enter the ergometer serial device'})
            return
        pulse_source = request.get('pulse_source', 'ergometer')
        if pulse_source not in ('ergometer', 'watch'):
            self._send_json(400, {'error': 'Choose a supported pulse source'})
            return
        heart_rate_address = request.get('heart_rate_address')
        if pulse_source == 'watch' and (
                not isinstance(heart_rate_address, text_types) or not heart_rate_address.strip()):
            self._send_json(400, {'error': 'Scan for and select a smartwatch first'})
            return

        directory = os.path.dirname(__file__)
        data_directory = os.path.join(directory, 'data')
        if not os.path.isdir(data_directory):
            os.makedirs(data_directory)

        stamp = _utcnow().strftime('%Y-%m-%dT%H-%M-%S')
        base_name = stamp
        suffix = 1
        while (os.path.exists(os.path.join(data_directory, base_name + '.json')) or
               os.path.exists(os.path.join(data_directory, '.' + base_name + '.live.jsonl')) or
               os.path.exists(os.path.join(data_directory, '.' + base_name + '.live.control.jsonl')) or
               os.path.exists(os.path.join(data_directory, '.' + base_name + '.live.hr.json'))):
            base_name = '%s-%d' % (stamp, suffix)
            suffix += 1

        log_json = os.path.join(data_directory, base_name + '.json')
        live_log_path = os.path.join(data_directory, '.' + base_name + '.live.jsonl')
        live_control_path = os.path.join(data_directory, '.' + base_name + '.live.control.jsonl')
        heart_rate_path = os.path.join(data_directory, '.' + base_name + '.live.hr.json') \
            if pulse_source == 'watch' else None
        temporary = tempfile.NamedTemporaryFile(
            mode='wb', prefix='pyergometer-', suffix='.xml', delete=False)
        try:
            temporary.write(session_xml)
        finally:
            temporary.close()

        command = [
            sys.executable,
            os.path.join(directory, 'pyergometer.py'),
            '--session', temporary.name,
            '--log', log_json
        ]
        command.extend(['--device-type', selected_type['id']])
        if selected_type['needs_port']:
            command.extend(['--device', device.strip()])

        try:
            with open(live_log_path, 'wb'):
                pass
        except IOError as error:
            os.remove(temporary.name)
            self._send_json(500, {'error': 'Could not create live pulse log: %s' % error})
            return
        try:
            with open(live_control_path, 'wb'):
                pass
        except IOError as error:
            os.remove(temporary.name)
            os.remove(live_log_path)
            self._send_json(500, {'error': 'Could not create live workout control: %s' % error})
            return
        command.extend(['--live-log', live_log_path])
        command.extend(['--live-control', live_control_path])
        command.append('--local-only')
        chart_plan = build_chart_plan(request)
        # The page sends pulse_limit false when the limit is switched off for this workout.
        limit = pulse_limit(chart_plan, request.get('pulse_limit') is not False)
        if limit:
            command.extend(['--pulse-limit', str(limit)])
        if heart_rate_path:
            try:
                with open(heart_rate_path, 'wb'):
                    pass
            except IOError as error:
                os.remove(temporary.name)
                os.remove(live_log_path)
                os.remove(live_control_path)
                self._send_json(500, {'error': 'Could not create smartwatch pulse file: %s' % error})
                return
            monitor = HeartRateMonitor(heart_rate_address.strip(), heart_rate_path)
            try:
                monitor.start()
            except OSError as error:
                os.remove(temporary.name)
                os.remove(live_log_path)
                os.remove(live_control_path)
                os.remove(heart_rate_path)
                self._send_json(500, {'error': 'Could not start Bluetooth monitor: %s' % error})
                return
            # A watt workout follows its plan without a pulse, so it starts at once and
            # the watch connects in the background. A pulse workout needs the pulse first.
            watch_ready = True
            if chart_plan['mode'] == 'pulse':
                # Waiting for the watch can take 30 seconds; let other requests run meanwhile.
                self.server.starting = True
                self.server.lock.release()
                try:
                    watch_ready = monitor.wait_for_sample(WATCH_START_TIMEOUT)
                finally:
                    self.server.lock.acquire()
                    self.server.starting = False
            if not watch_ready:
                # Read the reason before stopping; terminating the monitor would mask it as an exit code.
                watch_error = monitor.status()['error']
                monitor.stop()
                os.remove(temporary.name)
                os.remove(live_log_path)
                os.remove(live_control_path)
                if os.path.exists(heart_rate_path):
                    os.remove(heart_rate_path)
                if os.path.exists(heart_rate_path + '.status'):
                    os.remove(heart_rate_path + '.status')
                self._send_json(400, {
                    'error': 'Ingen puls fra uret inden for %d sekunder (%s). Slå urets '
                             'Bluetooth-pulsdeling til og prøv igen.' % (
                                 WATCH_START_TIMEOUT, watch_error or 'ingen forbindelse')
                })
                return
            command.extend(['--heart-rate-file', heart_rate_path])
        else:
            monitor = None
        try:
            process = subprocess.Popen(command, cwd=directory)
        except OSError as error:
            os.remove(temporary.name)
            os.remove(live_log_path)
            os.remove(live_control_path)
            if monitor:
                monitor.stop()
            if heart_rate_path and os.path.exists(heart_rate_path):
                os.remove(heart_rate_path)
            if heart_rate_path and os.path.exists(heart_rate_path + '.status'):
                os.remove(heart_rate_path + '.status')
            self._send_json(500, {'error': 'Could not start pyergometer: %s' % error})
            return

        self.server.process = process
        self.server.session_file = temporary.name
        self.server.log_json = log_json
        self.server.last_exit_code = None
        self.server.last_error = None
        self.server.stop_requested = False
        self.server.chart_plan = chart_plan
        self.server.chart_limited = False
        self.server.chart_live_path = live_log_path
        self.server.chart_control_path = live_control_path
        self.server.heart_rate_monitor = monitor
        self.server.heart_rate_path = heart_rate_path
        self.server.chart_file_position = 0
        self.server.chart_points = []
        self.server.demo_started_at = None
        self.server.demo_points = []
        self._send_json(200, {'running': True, 'log': base_name})

    def _discard_body(self):
        # Windows resets a connection closed with unread data, which can hide the error reply.
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return
        if 0 < length <= MAX_REQUEST_SIZE:
            self.rfile.read(length)

    def log_request(self, code='-', size='-'):
        # The page polls these every second or two; only log them when something goes wrong.
        try:
            succeeded = int(code) < 400
        except (TypeError, ValueError):
            succeeded = False
        path = urlparse(self.path).path
        if path == '/favicon.ico' or (
                succeeded and self.command == 'GET' and path in QUIET_PATHS):
            return
        BaseHTTPRequestHandler.log_request(self, code, size)

    def log_message(self, format, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), format % args))


def run_trainer(device=None, simulate=False, host='127.0.0.1', port=8765, open_browser=True,
                device_type=None):
    server = WorkoutServer((host, port), device=device, simulate=simulate,
                           device_type=device_type)
    port = server.server_address[1]
    url = 'http://127.0.0.1:%d/' % port
    print('Workout builder: %s' % url)
    if host not in LOOPBACK_HOSTS:
        for address in sorted(local_network_addresses()):
            if address[0].isdigit() and not address.startswith('127.'):
                print('On the local network: http://%s:%d/' % (address, port))
        print('Warning: anyone on this network can control the ergometer from the page.')
    if open_browser:
        opener = threading.Thread(target=webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping workout builder')
    finally:
        server.server_close()
