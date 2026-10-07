"""Small, deterministic helpers shared by capture, verification and history."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def stable_id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def norm(text):
    return re.sub(r"\s+", " ", str(text or "").casefold()).strip()


def canonical_url(url):
    """Comparison key only. Always keep the untouched observed URL alongside it."""
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(query), ""))
    except ValueError:
        return None


def public_navigation_url(url, allow_local=False):
    """Bound browser navigations to HTTP(S); reject embedded credentials/private hosts.

    The collector applies this to each main-document request, including redirects.
    This is a local-tool guard, not a replacement for an egress firewall in a SaaS.
    """
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return False
        if allow_local:
            return True
        if p.hostname.lower() == "localhost" or p.hostname.endswith(".local"):
            return False
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (ValueError, OSError):
        return False


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return str(path)


def content_hash(value):
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def redact_error(message, proxy=None):
    text = str(message)
    if proxy:
        text = text.replace(proxy, "[proxy redacted]")
        try:
            p = urlsplit(proxy)
            if p.password:
                text = text.replace(p.password, "[redacted]")
        except ValueError:
            pass
    return re.sub(r"(https?|socks5)://[^\s/@]+:[^\s/@]+@", r"\1://[redacted]@", text)[:2000]
