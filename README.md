pyergometer
===========

This program connects to a Kettler ergometer (tested with model E3)
through a serial connection and, following a user-specified program, controls the
ergometer power setting to keep the subject's heart rate at a desired level (which
may or may not vary during the program run).

The program can also run in "fixed" mode, i.e. setting power levels at
set times given in the user program.

Pulse, power and pedaling rpm are optionally recorded to a log file
that can be used to e.g. visualize the run afterwards.

This fork
---------

This is a fork of [kahara/pyergometer](https://github.com/kahara/pyergometer)
by Joni Kähärä. It keeps the original pulse control and adds a web page for
building and running workouts from a PC, phone or tablet, user profiles with
maximum pulse, smartwatch heart rate over Bluetooth LE, a live pulse chart,
and a driver template for adding other ergometers. The aim is to run it on a
Raspberry Pi next to the bike. The upload of sessions to the original author's
S3 bucket has been removed; all data stays on the local computer.

Quick start on Windows: double-click ``start.bat``. It starts the page on
``http://127.0.0.1:8765/`` and prints the address to use from other devices
on the same network.

Starting the program without a session file opens a local workout builder:

    ./pyergometer.py

Add ``--host 0.0.0.0`` to reach the page from phones and tablets on the local
network. Anyone on that network can then control the ergometer, so only use it
on a trusted network.

Install the Python 3 dependencies before starting the workout builder:

    python -m pip install -r requirements.txt

Choose a maximum pulse, add work intervals as a percentage of that maximum,
and add low-load breaks with a duration and watt setting. The page calculates
the target pulse for each interval and starts the existing control program.
Maximum pulse belongs to the user profile: choose it in 10-bpm increments or
calculate it from birth year with `208 - (0.7 * age)`; the estimate is rounded
to the nearest bpm. It is saved automatically when changed and cannot be
overridden for a single workout.
Create one user profile for each person using the ergometer. Maximum pulse,
workout setups and the selected smartwatch are stored separately for each
profile, so selecting a profile switches to that person's settings. Existing setups in
``workouts.json`` are kept under the initial ``Standard`` profile. New profile
data is stored locally in the Git-ignored ``user_profiles.json`` file.
Name and save workout setups locally, then load them again from that profile's
saved setup list. Presets contain only the ordered intervals and breaks, with
pulse targets as a percentage of maximum, so the same workout adapts to each
person's maximum pulse.
During a workout or livedemo, a heart above the chart shows the current pulse
in large digits together with the current target.
Switching user profiles clears the previous workout's chart and resets the
unsaved form so that one person's workout history is not shown for another.
The shared workout template list includes a 20-minute HIIT session: 4 minutes
at 55% of maximum pulse, 13 repetitions of 30 seconds at 85% plus 30 seconds
at 60%, then 3 minutes at the ergometer's minimum 25 W. The shared template named
``HIIT 30/30 (13 gentagelser)`` is
copied into the editable plan and cannot be overwritten; save a customized
version under a new name on the selected profile. Repeat groups can be adjusted
by changing their repetition count, work/recovery durations in seconds, and
work/recovery pulse targets. The controller receives each repeated phase at
its exact second boundary.
The pulse chart shows target intervals, measured pulse, and elapsed workout
time. If no maximum pulse has been selected yet, the plan is still shown using
pulse targets as percentages of maximum until a maximum pulse is chosen. While
setting up a workout, drag a blue target line vertically to change
an interval's target pulse, or drag the orange diamond at a block's end
horizontally to change its duration. The corresponding plan fields update and
are used when saving or starting the workout; during a livedemo, edits apply
to the next workout. During a real workout, the same chart controls send
changes to the ergometer immediately; the new target is used at the next
one-second control update, and a shortened workout ends at its updated finish
time. The start button changes to a red ``Stop træning`` button while active;
stopping sends a controlled command and reduces the ergometer load to 25 W.
Measurements are sampled and displayed about once per second. Use
``Afspil livedemo med simuleret puls`` to preview the chart without starting
the ergometer; demo measurements are synthetic and are not saved as workout
results.
As an alternative to the ergometer's pulse sensor, select ``Smartwatch via Bluetooth
LE`` and scan for the watch once while setting up its user profile. The selected
watch is remembered for that profile, and the app tries that saved device at
the next workout without scanning again. If the watch cannot be found or its
Bluetooth address has changed, scan and select it again. The watch must broadcast the standard Bluetooth
Heart Rate Service (UUID 180D); the workout page reads its heart-rate
notifications directly, without a phone relay. Enable the watch's heart-rate
broadcast mode before connecting. If another app is already connected to the
watch, close it if the workout page cannot connect. Pulse samples stay on the
local computer and are not uploaded. If the watch connection is lost, the page
reports the error and the ergometer holds its current load until fresh watch
readings resume.
The builder is available only on the local computer. Session logs are written
to ``data/`` with Windows-compatible timestamped filenames.

Supported bikes are listed in ``devices.py``: currently a Kettler on a serial
RS-232 port and a simulation. Choose the bike type on the page under the bike
settings, or pass ``--device-type`` on the command line (``--simulate`` is short
for ``--device-type simulator``). To add another bike, write a driver class with
the ``ErgometerDevice`` interface (``status()``, ``set_power()`` and the
``pulse``, ``rpm`` and ``power`` values) and add it to ``DEVICE_TYPES``; the
pulse control, chart and logs then work unchanged.

To use an existing XML session directly, pass it as before:

    ./pyergometer.py --session session.xml --device /dev/ttyUSB0

Motivation
----------

I could not get [jErgometer](https://github.com/xylo/JErgometer) to work in
Mac OS X 10.6. That is, the program basically worked ok but the Java serial comms
(RXTXserial) tended to be too unstable.

License
-------

GNU General Public License version 3 or later, as chosen by the original
author. See ``LICENSE``.
