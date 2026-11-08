"""Private historical effect classifications remain protected when a provider is absent.

These identifiers are compatibility data, not installed operations or provider imports.
The owner retains saved records; absent providers never grant an agent access.
"""
PRIVATE_SIGNOFF_OPERATIONS = frozenset({"whatsapp_send"})
PRIVATE_JOB_KINDS = frozenset({"whatsapp.send"})
