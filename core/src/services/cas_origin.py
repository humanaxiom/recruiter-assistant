"""Which browser origin a CAS round trip belongs to (fix/cas-local-origin).

One deployment is reached two ways: publicly through nginx
(``https://sfuai.ca:8000``) and, on the box itself, at ``localhost`` — the
public name does not resolve back to the box from inside the LAN. CAS needs a
service URL the browser can actually reach, so the static
``cas_service_base_url`` cannot serve both.

The local pair (``cas_local_service_base_url`` / ``cas_local_frontend_base_url``)
is chosen ONLY when the request's own ``Host`` is a loopback name. Nothing is
ever derived from the header beyond that yes/no: the URLs themselves always
come from settings, so a forged ``Host`` can at worst select the configured
localhost pair for the forger's own browser.
"""

from __future__ import annotations

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def is_loopback_host(host: str) -> bool:
    """True for ``localhost``/``127.0.0.1``/``[::1]``, with or without a port."""
    host = host.strip().lower()
    if host.startswith("["):
        name = host.split("]", 1)[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in _LOOPBACK_NAMES
