"""Ergometer drivers.

Every driver controls one kind of bike and has the same small interface, so the
pulse controller, web page and logs work with any of them:

    pulse   current pulse in bpm (0 when the bike has no pulse reading)
    rpm     current pedalling cadence
    power   current load in watts
    status()                         read fresh values from the bike
    set_power(absolute=0.0, relative=0.0)
                                     set the load in watts, or change it by
                                     ``relative`` watts
    close()                          release the connection (optional)

To add a bike, write a class with that interface (subclass ``ErgometerDevice``)
and register it in ``DEVICE_TYPES`` below. ``needs_port`` tells the web page
whether to ask for a serial port.
"""


class ErgometerDevice(object):
    """Base class documenting the interface every ergometer driver provides."""

    pulse = 0
    rpm = 0
    power = 0

    def status(self):
        raise NotImplementedError

    def set_power(self, absolute=0.0, relative=0.0):
        raise NotImplementedError

    def close(self):
        pass


def _create_kettler(port):
    from kettler import Kettler
    return Kettler(device=port)


def _create_simulator(port):
    from simulator import Simulator
    return Simulator()


# Order is the order shown on the web page.
DEVICE_TYPES = [
    {
        'id': 'kettler',
        'label': 'Kettler (seriel RS-232)',
        'needs_port': True,
        'create': _create_kettler,
    },
    {
        'id': 'simulator',
        'label': 'Simulation (ingen cykel)',
        'needs_port': False,
        'create': _create_simulator,
    },
]

DEFAULT_DEVICE_TYPE = 'kettler'


def device_type(type_id):
    for entry in DEVICE_TYPES:
        if entry['id'] == type_id:
            return entry
    raise ValueError('Unknown ergometer type: %s' % type_id)


def device_type_ids():
    return [entry['id'] for entry in DEVICE_TYPES]


def public_device_types():
    """The device list without factories, for the web page."""
    return [{'id': entry['id'], 'label': entry['label'], 'needs_port': entry['needs_port']}
            for entry in DEVICE_TYPES]


def create_device(type_id, port=None):
    entry = device_type(type_id)
    if entry['needs_port'] and not port:
        raise ValueError('%s needs a serial port' % entry['label'])
    return entry['create'](port)
