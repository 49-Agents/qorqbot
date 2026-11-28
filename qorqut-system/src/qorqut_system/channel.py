"""A private local channel between the parts of one installation, on the version this system uses.

Linux and macOS use a Unix socket in a private folder; Windows uses a named pipe open only to the current user. Both
carry the same control token. This module holds the operations the feature code needs around a channel: listen, connect,
find out who connected, make a token and compare one, and the form a channel takes inside an open private directory. What
travels over the channel stays with the feature code.

The caller owns the privacy of the folder the channel's path lies in; the channel itself is given to this user only.
"""

from __future__ import annotations

import hmac
import secrets
import sys

if sys.platform == "win32":
    from .channel_windows import (check_caller, connect, connect_in_directory, listen, listen_here,
                                  listen_in_directory, new_socket, peer, remove_in_directory, connect_socket)
else:
    from .channel_unix import (check_caller, connect, connect_in_directory, connect_socket, listen, listen_here,
                               listen_in_directory, new_socket, peer, remove_in_directory)

TOKEN_BYTES = 32


def new_token() -> str:
    """A fresh control token (today's line in the managed launcher)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_matches(expected, given) -> bool:
    """Whether a request carries the expected token, compared in constant time (today's line in the managed host)."""
    if not isinstance(expected, str) or not isinstance(given, str):
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


__all__ = ["check_caller", "connect", "connect_in_directory", "connect_socket", "listen", "listen_here", "listen_in_directory", "new_socket",
           "new_token", "peer", "remove_in_directory", "token_matches"]