# Fastmail connector instructions

Optional. Not selected by any default role.

The default `calendar` skill used to say: "a Fastmail calendar exists only if the optional Fastmail
package supplies it." The default skill now says only that the connections returned by
`calendar_calendars_list` are the ones to use, which covers every provider, so the Fastmail package
needs to add nothing to the role text.

Fastmail mail guidance ships in the optional provider package at
`qorqut_fastmail/resources/email-management/providers/fastmail.md`. Core Email Management
contains provider-neutral guidance. Installing the provider resource does not overwrite
the owner's canonical guide; installer adoption remains separate work.
