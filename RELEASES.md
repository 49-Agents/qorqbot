# Release artifacts

The first release targets Ubuntu 24.04 on Intel/AMD 64-bit machines. The normal
application download is a versioned TAR.GZ archive with the installation guide.
External programs are prerequisites supplied by the user.

A release should provide:

- The versioned application archive.
- `SHA256SUMS`, covering each downloadable artifact.
- Release notes identifying tested platform/prerequisite versions, changes,
  migration requirements and known limitations.
- The exact corresponding source tag and required third-party source artifacts.
- Separately versioned optional connector packages and their setup instructions.
- Applicable license notices and the release's entry in `RELEASE-LICENSES.json`.

Downloads and setup instructions are available at
[qorqbot.com](https://qorqbot.com/) and
[49agents.com/qorqbot](https://49agents.com/qorqbot/). Native Windows host
acceptance and native macOS 14 acceptance remain pending.

GitHub Releases hosts versioned releases, including
[1.3.2](https://github.com/49-Agents/qorqbot/releases/tag/v1.3.2),
published November 14, 2026. A release
page can host both the application and separate
connector assets, with a short index linking to each connector's setup guide.
This requires no separate marketplace or automatic extension registry. The
WhatsApp bundle must be accompanied by the exact helper corresponding-source
archive and its build instructions. Preserve the separate Apache 2.0 adapter/UI
and GPL-3.0-only helper notices alongside dependency notices.

Verify the checksum from the trusted release channel before installation. A
checksum detects file changes; its authenticity depends on how it is obtained.
Use the installation/update instructions for the exact release and keep user
state intact. Package replacement alone does not establish database migration
compatibility.

The [release runbook](qorqut-code/docs/distribution/RELEASE-RUNBOOK.md) describes
build and acceptance steps. Publication is a distinct maintainer action after
source, artifact and license checks are complete. The release's scope and
platform limitations are described in
[1.3.2](qorqut-code/docs/distribution/RELEASE-1.3.2.md); the earlier
[1.3.1](qorqut-code/docs/distribution/RELEASE-1.3.1.md) and
[1.3.0](qorqut-code/docs/distribution/RELEASE-1.3.0.md) have their own.

Ordinary backend releases do not require rebuilding these downloads. See
[CI and release preparation](qorqut-code/docs/CI-AND-RELEASES.md).
