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
- Applicable license notices and the fixed license conversion date for the release.

GitHub Releases is the selected first download channel. No public repository or
download URL is configured yet. A release page can host both the application and separate
connector assets, with a short index linking to each connector's setup guide.
This requires no separate marketplace or automatic extension registry. The
WhatsApp native helper binary is currently withheld pending dependency licensing;
do not list it as an available release download until that is resolved.

Verify the checksum from the trusted release channel before installation. A
checksum detects file changes; its authenticity depends on how it is obtained.
Use the installation/update instructions for the exact release and keep user
state intact. Package replacement alone does not establish database migration
compatibility.

The [release runbook](qorqut-code/docs/distribution/RELEASE-RUNBOOK.md) describes
build and acceptance steps. Publication is a distinct maintainer action after
source, artifact and license checks are complete.
