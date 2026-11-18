"""Is the public hostname inside a DNS zone that Cloudflare serves? Checked before anything is installed or logged in.

WHAT: walks up the hostname (`a.b.example.com`, `b.example.com`, `example.com`) asking for NS records over DNS-over-HTTPS
(Cloudflare's public resolver, JSON API) and reports the first name that has its own NS set: that is the zone, and it is on
Cloudflare when every nameserver is `*.ns.cloudflare.com`. A named tunnel can only be reached through a proxied CNAME in such a
zone.
WHY DoH and not the system resolver: Python's stdlib cannot ask for NS records, and the answer must not depend on a local cache.
INVARIANTS: a failed lookup is "unknown", never "not on Cloudflare": the caller warns and goes on (the DNS route step proves it
anyway).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from computerlib.targets.chatgpt.shim.jsonish import as_list, as_object

DOH_URL = "https://cloudflare-dns.com/dns-query"
CLOUDFLARE_NAMESERVERS = ".ns.cloudflare.com"
NS_TYPE = 2

Resolver = Callable[[str], object | None]  # name -> parsed DoH JSON answer for its NS records, None when the lookup failed


def doh_ns(name: str) -> object | None:
    query = urllib.parse.urlencode({"name": name, "type": "NS"})
    request = urllib.request.Request(f"{DOH_URL}?{query}", headers={"Accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class Zone:
    name: str  # "" when no zone was found
    nameservers: tuple[str, ...]
    on_cloudflare: bool | None  # None: the lookup failed, nothing is known


def _nameservers(answer: object, name: str) -> tuple[str, ...]:
    found: list[str] = []
    for record in as_list(as_object(answer).get("Answer")):
        fields = as_object(record)
        owner = str(fields.get("name", "")).rstrip(".").lower()
        if fields.get("type") == NS_TYPE and owner == name:
            found.append(str(fields.get("data", "")).rstrip(".").lower())
    return tuple(sorted(found))


def find_zone(hostname: str, resolve: Resolver | None = None) -> Zone:
    resolve = resolve or doh_ns  # resolved at call time so a test can stand in for the network
    labels = hostname.lower().rstrip(".").split(".")
    for start in range(len(labels) - 1):  # never ask about the bare TLD
        candidate = ".".join(labels[start:])
        answer = resolve(candidate)
        if answer is None:
            return Zone("", (), None)
        nameservers = _nameservers(answer, candidate)
        if nameservers:
            return Zone(candidate, nameservers, all(ns.endswith(CLOUDFLARE_NAMESERVERS) for ns in nameservers))
    return Zone("", (), False)
