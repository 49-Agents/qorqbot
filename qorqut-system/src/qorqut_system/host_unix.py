"""The mount table and the interface addresses on Linux: today's lines, moved from the one caller that reads them.

``mounts`` parses ``/proc/self/mountinfo`` into one record per mount, in kernel order, with the three path fields
unescaped as the kernel writes them. ``local_addresses`` reads the host's own addresses from ``/proc/net/fib_trie``
and ``/proc/net/if_inet6``, so a non-loopback alias of this host is found as well as the loopback one.

Nothing is cached: a mount or an address that appeared since the last call is reported. A table that cannot be read
raises what reading it raises, so the caller keeps the failure it had before the move.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
import re


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def mounts():
    """Every mount of this machine: its id, root, mountpoint, options, filesystem and source, in kernel order."""
    result = []
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            pre, post = line.rstrip().split(' - ', 1)
            a, b = pre.split(), post.split()
            result.append({'mount_id': a[0], 'root': _unescape(a[3]), 'mountpoint': _unescape(a[4]),
                           'options': a[5].split(',') + b[2].split(','), 'filesystem': b[0],
                           'source': _unescape(b[1])})
    return result


def local_addresses():
    """All Linux interface addresses, including non-loopback IP aliases of this host."""
    lines = Path('/proc/net/fib_trie').read_text().splitlines()
    addresses = {str(ipaddress.ip_address(lines[i - 1].split()[-1]))
                 for i, line in enumerate(lines) if i and '/32 host LOCAL' in line}
    addresses.update(str(ipaddress.IPv6Address(int(line.split()[0], 16)))
                     for line in Path('/proc/net/if_inet6').read_text().splitlines())
    return addresses