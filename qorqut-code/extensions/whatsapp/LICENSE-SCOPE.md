# Optional WhatsApp licensing

The Python adapter, guidance and UI assets are first-party Qorqbot material
under the accompanying BSL 1.1. The native helper is a separate executable:
its server exchanges JSON over Unix sockets (including HTTP for managed sends),
and its independent MCP client uses standard input/output.

The helper links `go.mau.fi/libsignal v0.2.2` (GPLv3), and uses Whatsmeow and
go.mau.fi/util (MPL-2.0), plus other independently licensed modules. The helper
must not be presented as BSL-only. Its source directory and generated helper
binary are excluded from the adapter's BSL grant pending a separately stated
helper license. The optional binary is not cleared for public distribution
until that choice and the corresponding-source delivery are complete.

License notices already bundled in a wheel do not replace the obligation to
provide the appropriate source. The main Qorqbot download does not include this
optional executable.
