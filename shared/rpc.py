"""RPC endpoint naming without leaking the endpoint's secret.

A dedicated RPC URL carries its API key in the path (`/v2/<key>`), the query
string (`?apikey=`), userinfo, or an endpoint-specific subdomain. Only the
**host** is ever recorded — in a CSV, a log line, `/health`, a benchmark file.
`rpc_host()` is the one way to get it; `redact()` scrubs the rest of the URL out
of free text (exception messages from `requests` quote the path:
"Max retries exceeded with url: /v2/<key>").

Shared by the gateway (health, logs), the runner (rpcHost column) and the RPC
benchmark, so all three name a provider the same way.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlsplit

REDACTED = "<redacted>"
BASELINE_PROVIDER = "publicnode"
"""Every M5 row before the provider column existed ran against publicnode
(docs/measurements.md, M3 cleanup §2), so a missing host means this."""

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_MIN_SECRET_LEN = 6  # shorter path segments ("v2", "rpc") are not secrets


def rpc_host(url: str | None) -> str | None:
    """Hostname of an RPC URL, lower-case, nothing else. None if there is none."""
    if not url:
        return None
    text = url.strip()
    if "://" not in text:
        text = "http://" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    return host or None


def provider_of(host: str | None) -> str:
    """Short provider label for a host: the condition value analyze.py groups on.

    Missing host → the baseline (legacy rows). publicnode → "publicnode",
    loopback → "local", otherwise the second-level domain label
    ("base-sepolia.g.alchemy.com" → "alchemy", "x.base-sepolia.quiknode.pro"
    → "quiknode").
    """
    if host is None or (isinstance(host, float)) or not str(host).strip():
        return BASELINE_PROVIDER
    host = str(host).strip().lower()
    if "publicnode" in host:
        return BASELINE_PROVIDER
    if host in _LOOPBACK or host.startswith("127."):
        return "local"
    parts = [p for p in host.split(".") if p]
    return parts[-2] if len(parts) >= 2 else host


def secrets_of(url: str | None) -> list[str]:
    """Substrings of `url` that must never appear in output, longest first."""
    if not url:
        return []
    text = url.strip()
    try:
        parts = urlsplit(text if "://" in text else "http://" + text)
    except ValueError:
        return [text]
    out = {text, text.rstrip("/")}
    host = parts.hostname or ""
    # Everything after the host: path, query, fragment.
    tail = text.split(host, 1)[1] if host and host in text else ""
    tail = re.sub(r"^:\d+", "", tail)  # port is not secret
    if tail.strip("/"):
        out.update({tail, tail.rstrip("/")})
    if parts.path.strip("/"):
        out.update({parts.path, parts.path.rstrip("/")})
        for seg in parts.path.split("/"):
            if len(seg) >= _MIN_SECRET_LEN:
                out.update({seg, unquote(seg)})
    if parts.query:
        out.add(parts.query)
        for pair in parts.query.split("&"):
            value = pair.split("=", 1)[-1]
            if len(value) >= _MIN_SECRET_LEN:
                out.add(value)
    for cred in (parts.username, parts.password):
        if cred:
            out.add(cred)
    return sorted((s for s in out if s), key=len, reverse=True)


def redact(text: str | None, url: str | None) -> str | None:
    """`text` with every secret part of `url` replaced; the host survives."""
    if text is None or not url:
        return text
    host = rpc_host(url)
    full = url.strip()
    if full in text:
        text = text.replace(full, f"<rpc {host}>")
    for secret in secrets_of(url):
        text = text.replace(secret, REDACTED)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs an RPC URL's secret parts from every record.

    Attached to *handlers* (a logger-level filter does not see records that
    propagate up from child loggers). Formats the message and any traceback
    eagerly, redacts both, and leaves the record otherwise intact.
    """

    def __init__(self, url: str | None) -> None:
        super().__init__()
        self.url = url
        self.active = bool(secrets_of(url)) and bool(url)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.active:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never let logging crash the gateway
            return True
        scrubbed = redact(message, self.url)
        if scrubbed != message:
            # Only touch records that leaked: some formatters (uvicorn's access
            # log) unpack `record.args` and break if it is rewritten.
            record.msg = scrubbed
            record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.url)
        return True


def install_log_redaction(url: str | None, logger_names: tuple[str, ...] = ("", "uvicorn", "uvicorn.error", "uvicorn.access")) -> int:
    """Attach a `RedactingFilter` for `url` to the handlers of the named loggers.

    Returns how many handlers got it. Idempotent per handler and URL.
    """
    count = 0
    for name in logger_names:
        for handler in logging.getLogger(name).handlers:
            if any(isinstance(f, RedactingFilter) and f.url == url for f in handler.filters):
                continue
            handler.addFilter(RedactingFilter(url))
            count += 1
    return count
