#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Control and logging program for Kettler ergometers"""

__appname__ = "pyergometer"
__author__  = "Joni Kähärä (kahara)"
__version__ = "0.0pre0"
__license__ = "GNU GPL 3.0 or later"

import logging
log = logging.getLogger(__name__)

import argparse
import sys
from session import Session
from devices import DEFAULT_DEVICE_TYPE, create_device, device_type, device_type_ids
from ergometer import Ergometer

if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version="v%s" % __version__)
    
    parser.add_argument('-s', '--session', action='store', dest='session', default=None, help='Use session file SESSION (jErgometer-like XML format)')
    parser.add_argument('-d', '--device', action='store', dest='device', default=None, help='Use serial device DEVICE')
    parser.add_argument('-l', '--log', action='store', dest='log', default=None, help='Record session stats to log file LOG')
    parser.add_argument('--live-log', action='store', dest='live_log', default=None, help='Write live pulse samples to JSON lines file')
    parser.add_argument('--live-control', action='store', dest='live_control', default=None, help='Read live workout updates from JSON lines file')
    parser.add_argument('--heart-rate-file', action='store', dest='heart_rate_file', default=None, help='Read Bluetooth heart-rate samples from JSON file')
    parser.add_argument('--local-only', action='store_true', dest='local_only', help='Accepted for compatibility; nothing is uploaded')
    parser.add_argument('--host', action='store', dest='host', default='127.0.0.1', help='Workout builder address; use 0.0.0.0 to allow tablets and phones on the local network')
    parser.add_argument('--port', action='store', dest='port', type=int, default=8765, help='Workout builder port (default 8765)')
    parser.add_argument('--no-browser', action='store_false', dest='open_browser', help='Do not open the workout builder in a local browser')
    parser.add_argument('-t', '--device-type', action='store', dest='device_type', default=None, choices=device_type_ids(), help='Ergometer type (default: %s)' % DEFAULT_DEVICE_TYPE)
    parser.add_argument('-x', '--simulate', action='store_true', dest='simulate', default=None, help='Run against a simulation; same as --device-type simulator')
    
    args  = parser.parse_args()
    
    if not args.session:
        from workout import run_trainer
        run_trainer(device=args.device, simulate=args.simulate, host=args.host,
                    port=args.port, open_browser=args.open_browser,
                    device_type=args.device_type)
        sys.exit(0)
    
    session = Session(session=args.session)
    
    type_id = 'simulator' if args.simulate else (args.device_type or DEFAULT_DEVICE_TYPE)
    if device_type(type_id)['needs_port'] and not args.device:
        parser.print_help()
        sys.exit(2)
    ergometer = Ergometer(device=create_device(type_id, args.device), session=session,
                          log=args.log, live_log=args.live_log,
                          live_control=args.live_control,
                          heart_rate_file=args.heart_rate_file)
    
    ergometer.run()
