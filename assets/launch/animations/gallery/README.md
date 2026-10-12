# Shared animation gallery

The five Opus/max candidates are shown in the existing content-gallery template. This local copy supports sandboxed self-contained HTML animations, 3-column previews and Previous/Next playback. Existing candidate feedback, votes and saved review rounds are retained.

Persistent gallery: /home/alp/qorqbot-launch/animation-gallery
Template: /home/alp/bazarlyk/content-creation/templates/content-gallery
The html-preview.patch records the presentation-only changes to that template; it embeds the licensed existing Zen Light font to avoid a remote font dependency. Review journals stay outside Git.

The browser control tool returned no available browsers during assembly. Runtime serving and static/DOM checks are distinct from visually verified playback. The owner can inspect the five drafts in the gallery.

Live URL: http://qorqut:4211/
Bound address: 100.98.122.123:4211 (Tailscale interface only).
Server: tmux socket `qorqbot-launch`, session `animation-gallery`.
Restart command: `/usr/bin/python3 -u /home/alp/qorqbot-launch/animation-gallery/gallery/server.py --host 100.98.122.123 --port 4211`.

`html-serving.patch` enables only the five named animation entries as HTML previews, retaining a script-only sandbox and blocking external assets/connections. Existing snapshot integrity checks remain intact. All candidate HTML bytes are identical to their workers' completed commits and to the gallery's frozen review snapshots.

Manager checks: 18 animation script blocks plus the gallery scripts parse; five snapshot URLs return HTML with matching SHA-256; three-column/two-row previews and Open/Next/Previous/Close pass in jsdom (no iframe playback); candidate and round feedback survive reload/server restart in an isolated copy. No test feedback was written to the live gallery. The host resolves and serves the user URL; remote-device reachability and real-browser visual playback remain unverified because no browser was exposed to the control tool.

The shared gallery is a local isolated review instance using the existing template, not the unavailable managed content-session integration. Session `764374b5-47f5-4e8c-8628-d08d28f07b3c`, active round `round-001`, five variants × one option. Owner choices remain unset.
