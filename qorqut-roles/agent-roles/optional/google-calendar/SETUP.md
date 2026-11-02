# Google calendar connection and limits

Optional. Not selected by any default role; the Google calendar package supplies it. These are
the Google-specific parts of the former default calendar skill, moved here unchanged.

## Google calendar limits

- The Google connection covers the account's primary calendar. Do not promise secondary or shared
  calendars.
- Google events can get a Meet link.

## Set up the native Google connection

Do this when the owner asks, for example "set up my Qorqut calendar". Qorqut's own Google connection
stays the way to connect Google Calendar. It is an OAuth client plus the owner's one-time refresh
authorization, not an API key, and it asks for calendar access only, not Gmail. Gmail authorization and
Google Calendar authorization are separate unless the runtime explicitly supplies both.

1. **Browser.** Use browser-control with the owner's Google account. The owner does the sign-in,
   verification and consent. If one blocks you, ask through owner-input; never ask for or type a
   password or code.
2. **Google Cloud.** In the current console UI, create or choose a project, enable the Google Calendar
   API, configure the OAuth consent screen and allow the owner's account, then create an OAuth client of
   type **Desktop app** and download its JSON to a private place outside Git and project repositories.
   Ask the owner about the consent publication status: an app left in external Testing can have
   expiring refresh tokens.
3. **Helper.** Setup needs the Google Calendar extension to be selected and available on the Qorqut
   host; connecting an account is optional and never required for core calendar work. If the extension
   is not available, say so and stop. Run the extension's setup helper on the host with the
   installation's dependencies: `python -m qorqut_google_calendar.connect --name <connection name>
   --google-client <downloaded client JSON> --config <private calendar.json>`. The config is the
   installation's private `calendar.json` (default `<workspace>/config/calendar.json`, mode 0600, or the
   path in `QORQUT_CALENDAR_CONFIG`). The helper serves a loopback callback (port 8765 unless `--port`
   changes it) on the machine that runs the browser, stores the primary calendar identity and refresh
   token without printing them, and creates no event. The owner approves the consent screen it opens.
   If you cannot run it, find the config path or reach that machine, report the exact gap and stop; do not
   invent a path or write credentials by hand.
4. **Verify read access.** `calendar_calendars_list` must show the connection and its primary calendar
   without a connection error, and `calendar_events_list` must read a short window. Create no test event
   unless the owner asks for one, then follow the Signoff rule above. Say what you actually saw before
   calling it connected.

Keep the client file, client secret and tokens out of Tasks, notes, messages, Context and chat.
