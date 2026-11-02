# WhatsApp helper source availability

The optional WhatsApp wheel includes a separate GPL-3.0-only native executable.
The matching complete source is supplied alongside its connector download as:

- Linux x86-64: `qorqut-whatsapp-0.1.0-linux-x86_64-source.tar.gz`
- Apple Silicon, when natively built and qualified: `qorqut-whatsapp-0.1.0-macos-arm64-source.tar.gz`

The source archive must match the binary platform; a Linux helper is not a macOS
binary. Native macOS build and runtime qualification are separate release checks.

That archive contains the native helper source, its license, go.mod/go.sum,
all 29 linked third-party module source trees with their original notices, and
instructions/scripts for rebuilding the helper with the recorded Go toolchain.
Its SOURCE-MANIFEST.json records the exact executable hash and source inventory.
No account credentials are required to build it.

Distribute the matching source archive alongside the executable archive with
equivalent access. The source and GPL rights remain available independently of
Qorqbot's BSL terms. License notices alone are not a substitute for corresponding
source. The Python adapter and UI use Apache 2.0 as explained in LICENSE-SCOPE.md.
