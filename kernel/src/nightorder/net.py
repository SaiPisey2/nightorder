"""Outbound URL checks for worker-initiated requests.

Gate notifications are delivered to a URL that comes from the pipeline spec.
The worker sits inside the cluster, so a spec that names an internal address
turns the worker into a request proxy for whatever it can reach — most
sharply the cloud metadata service, which hands out credentials to anything
that asks from the right network position.

Specs are written by trusted project members, but the control plane is open
unless NIGHTORDER_AUTH=on, so "trusted" is weaker than it sounds. These checks
cost one DNS lookup per delivery.

Loopback, link-local, multicast and reserved addresses are refused. Private
ranges are allowed, because a self-hosted relay on a private network is a
normal deployment. Set NIGHTORDER_ALLOW_LOCAL_WEBHOOKS=1 to permit loopback
and link-local as well, which is for local development.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")

# Names that resolve to a metadata service on the major clouds.
BLOCKED_HOSTNAMES = {
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}


class BlockedURL(ValueError):
    """Raised when an outbound URL points somewhere the worker must not reach."""


def _local_allowed() -> bool:
    return os.environ.get("NIGHTORDER_ALLOW_LOCAL_WEBHOOKS", "") == "1"


def _refuse_address(ip: ipaddress._BaseAddress) -> str | None:
    """Return a reason to refuse this address, or None if it is acceptable."""
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_unspecified:
        return "an unspecified address"
    if _local_allowed():
        return None
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address (this is where cloud metadata lives)"
    if ip.is_reserved:
        return "a reserved address"
    return None


def resolve_addresses(hostname: str) -> list[ipaddress._BaseAddress]:
    """Every address `hostname` resolves to. Raises BlockedURL if it resolves to none."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise BlockedURL(f"could not resolve '{hostname}': {e}") from e
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise BlockedURL(f"'{hostname}' resolved to no usable address")
    return addresses


def check_outbound_url(url: str) -> None:
    """Raise BlockedURL if the worker must not send a request to `url`.

    Every resolved address is checked, not just the first: a name that returns
    one public address and one link-local address is refused.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(
            f"scheme '{parsed.scheme or '(none)'}' is not allowed; use "
            + " or ".join(ALLOWED_SCHEMES)
        )
    hostname = parsed.hostname
    if not hostname:
        raise BlockedURL("url has no host")
    if hostname.lower().rstrip(".") in BLOCKED_HOSTNAMES:
        raise BlockedURL(f"'{hostname}' is a metadata service address")

    for ip in resolve_addresses(hostname):
        reason = _refuse_address(ip)
        if reason is not None:
            raise BlockedURL(
                f"'{hostname}' resolves to {ip}, which is {reason}. "
                "Set NIGHTORDER_ALLOW_LOCAL_WEBHOOKS=1 if this is local development."
            )
