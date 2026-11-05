# Qorqut Guacamole click focus extension

This Guacamole 1.6.0 web extension restores browser focus to the Guacamole
iframe when the remote display is pressed. It also adds a compact clipboard
button. Guacamole's display handler then continues to receive the original
mouse event and may prevent its default action as usual. The extension does
not handle hover or alter remote-display clicks. Clipboard controls stop their
local input events before Guacamole receives them; browser typing, selection,
paste and composition keep their normal defaults. Only Send to desktop forwards
the field content through the native clipboard service.

The clipboard popover has two explicit actions:

- **Copy** selects the latest text received from the remote desktop and tries
  the browser clipboard write. If browser clipboard access is denied, the
  text remains selected for a user-approved Ctrl+C.
- **Send to desktop** sends the text in the field through Guacamole's native
  `clipboardService`. The user can paste into the field with Ctrl+V first.

The extension tracks remote text only in page memory from `guacClipboard`
events. It does not poll or persist clipboard contents, automatically type
clipboard text into a terminal, or submit a command.

The deployed dashboard and Guacamole URLs use HTTP. In that context
`navigator.clipboard` may be unavailable regardless of browser permission.
The field remains the explicit Ctrl+C/Ctrl+V fallback; granting permission
alone does not turn an HTTP origin into a secure clipboard context.

The source follows Guacamole 1.6.0's extension contract: `guac-manifest.json`
is at the root of the JAR and declares the JavaScript and CSS resources. The
display selector is the 1.6.0 client container, `.client-main .display`.

## Build

From this directory, run:

```sh
python3 build-artifact.py
```

The builder writes `dist/guacamole-click-focus-1.0.0.jar` with sorted, fixed
timestamps and stable ZIP metadata. Pass `--output PATH` to choose another
artifact path. Running the builder twice over unchanged sources produces the
same SHA-256.

## Install

Guacamole loads extension JARs from `GUACAMOLE_HOME/extensions` at webapp
startup. For the Docker deployment, expose a host directory containing the
artifact as a read-only `/etc/guacamole/extensions` volume on the `guacamole`
service, then recreate only that web container during an approved maintenance
window:

```yaml
volumes:
  - ./extensions:/etc/guacamole/extensions:ro
```

```sh
install -d extensions
install -m 0644 dist/guacamole-click-focus-1.0.0.jar extensions/
docker compose --env-file secrets.env up -d --force-recreate guacamole
```

Verify the Guacamole webapp log reports the `qorqut-click-focus` namespace and
that a remote display click still reaches the desktop.

## Rollback

Keep the previous extension directory and artifact receipt. Remove only this
JAR from the mounted `extensions` directory and recreate the Guacamole web
container:

```sh
rm extensions/guacamole-click-focus-1.0.0.jar
docker compose --env-file secrets.env up -d --force-recreate guacamole
```

This extension has no database, guacd, PostgreSQL, dashboard, or connection
configuration changes. Recreating Guacamole disconnects its web sessions;
restore the previous artifact before reconnecting if the extension is the
cause of a failure.

Additional reconnect/IME input coverage: `tests/test_clipboard_reconnect_input_browser.js`.
