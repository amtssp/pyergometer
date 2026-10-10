#!/usr/bin/env python

import datetime
import json
import sys


def main(path):
    try:
        with open(path, 'r') as log_file:
            log = json.load(log_file)
        timestamp = datetime.datetime.strptime(
            log['begin'], '%Y-%m-%dT%H:%M:%S')
        steps = log['steps']
    except (IOError, ValueError, KeyError, TypeError) as error:
        sys.stderr.write('Could not read workout log: %s\n' % error)
        return 1

    sys.stdout.write('Time,Power (W),Rpm,Pulse,Target pulse\n')
    for step in steps:
        sys.stdout.write('%s,%d,%d,%d,%d\n' % (
            timestamp.strftime('%Y-%m-%dT%H:%M:%S'),
            int(step['power']),
            int(step['rpm']),
            int(step['pulse']),
            int(step['target_pulse'])
        ))
        timestamp += datetime.timedelta(seconds=1)
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.stderr.write('Usage: dumplog.py LOG.json\n')
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
