# Google calendar connector instructions

Optional. Not selected by any default role.

`SETUP.md` holds the guidance that used to be part of the default `calendar` skill and is specific
to Google: the "Set up the native Google connection" procedure and the Google calendar limits
(primary calendar only, Meet links). The default `calendar` skill keeps the provider-neutral
`calendar_*` workflow and the guest-notification Signoff rule.

## What the Google calendar package supplies

- `SETUP.md`, surfaced to Ordubashy when the package is installed, for example as a second skill or
  as a section of the `calendar` skill; a skill needs its own `bazarlyk-role-<id>` name.
- The setup helper, `python -m qorqut_google_calendar.connect` with `--name`, `--google-client`,
  `--config` and its other options. `SETUP.md` names this command and says setup requires the
  Google Calendar extension to be selected and available; connecting an account stays optional.
  It still names the private `calendar.json` under the installation workspace and
  `QORQUT_CALENDAR_CONFIG`; the package must confirm or update that config path.

No grant is needed: the `calendar_*` operations are core and stay in the default Ordubashy
`role.yaml`.
