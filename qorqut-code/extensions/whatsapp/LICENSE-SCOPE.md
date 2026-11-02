# Optional WhatsApp licenses

Copyright (c) 49 Agents LLC.

The Python adapter, agent guidance and UI assets are licensed under the Apache
License, Version 2.0, in `LICENSE`. They retain that license when imported into
Qorqbot; the application's separate BSL terms remain unchanged.

The independently runnable native helper in `native/` and the distributed
`qorqut_whatsapp/bin/whatsapp-personal` executable are licensed under GNU GPL
version 3 only. See `native/LICENSE` in source and `LICENSES/GPL-3.0.txt` in the
wheel's license metadata. The helper links GPLv3 libsignal, MPL-2.0 Whatsmeow and
go.mau.fi/util, and other separately licensed modules. Their original copyrights,
license texts and notices remain applicable and are retained with source and in
the wheel's `qorqut_whatsapp/third_party/` directory.

The helper runs as a separate executable. The Python adapter communicates with
its server through JSON over a Unix socket, including HTTP for managed sends;
the helper's independent MCP client uses standard input/output. The adapter does
not load the helper as a Python extension or shared library. The executable can
also be built and run independently of Qorqbot.

The wheel contains both programs, so its metadata reports
`Apache-2.0 AND GPL-3.0-only`. This describes the combined archive; it does not
relicense the Python adapter or the core application under GPL. Complete matching
helper source and build instructions accompany the optional download as described
in `SOURCE-AVAILABILITY.md`.
