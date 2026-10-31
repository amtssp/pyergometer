# pyergometer

Controller and web UI for a Kettler E3 ergometer, running on a Raspberry Pi.

- Run tests with `py -m unittest test_workout` (Windows has no `python`).
- `lang/da.json` and `lang/en.json` must have identical keys and placeholders; a test enforces it.
- New device drivers subclass `ErgometerDevice` and register in `DEVICE_TYPES` in `devices.py`.
- Keep README.md in step with user-visible features.
- After a feature: run the tests, commit, push to `origin master`, then update the Pi (see CLAUDE.local.md).
