"""shared/rpc.py — host extraction and redaction of RPC URLs (exp/rpc)."""

import logging
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from shared.rpc import (  # noqa: E402
    RedactingFilter,
    provider_of,
    redact,
    rpc_host,
    secrets_of,
)

KEY = "AbCdEf0123456789xyzKEY"
URLS = [
    f"https://base-sepolia.g.alchemy.com/v2/{KEY}",
    f"https://base-sepolia.infura.io/v3/{KEY}/",
    f"https://name-x.base-sepolia.quiknode.pro/{KEY}/",
    f"https://rpc.example.org/?apikey={KEY}",
    f"https://user:{KEY}@rpc.example.org/",
]


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://base-sepolia-rpc.publicnode.com", "base-sepolia-rpc.publicnode.com"),
        (f"https://base-sepolia.g.alchemy.com/v2/{KEY}", "base-sepolia.g.alchemy.com"),
        (f"https://user:{KEY}@rpc.example.org:8443/x?y=1", "rpc.example.org"),
        ("http://127.0.0.1:8552", "127.0.0.1"),
        ("127.0.0.1:8552", "127.0.0.1"),
        ("", None),
        (None, None),
    ],
)
def test_rpc_host_is_hostname_only(url, host):
    assert rpc_host(url) == host


@pytest.mark.parametrize("url", URLS)
def test_host_never_contains_the_key(url):
    assert KEY not in (rpc_host(url) or "")


@pytest.mark.parametrize(
    "host,provider",
    [
        (None, "publicnode"),
        (float("nan"), "publicnode"),  # legacy CSV row, pandas NaN
        ("", "publicnode"),
        ("base-sepolia-rpc.publicnode.com", "publicnode"),
        ("base-sepolia.g.alchemy.com", "alchemy"),
        ("name-x.base-sepolia.quiknode.pro", "quiknode"),
        ("127.0.0.1", "local"),
        ("localhost", "local"),
    ],
)
def test_provider_of(host, provider):
    assert provider_of(host) == provider


@pytest.mark.parametrize("url", URLS)
def test_redact_removes_key_in_every_shape_requests_quotes(url):
    host = rpc_host(url)
    path = url.split(host, 1)[1]
    texts = [
        f"HTTPSConnectionPool(host='{host}', port=443): Max retries exceeded with url: {path}",
        f"Could not connect to {url}",
        f"404 Client Error: Not Found for url: {url}",
        f"bare key {KEY} somewhere",
    ]
    for text in texts:
        out = redact(text, url)
        assert KEY not in out, out
    assert host in redact(f"Could not connect to {url}", url)


def test_redact_is_a_noop_for_a_keyless_url():
    url = "https://base-sepolia-rpc.publicnode.com"
    assert secrets_of(url) == [url]
    assert redact("timeout talking to base-sepolia-rpc.publicnode.com", url) == (
        "timeout talking to base-sepolia-rpc.publicnode.com"
    )


def test_filter_leaves_clean_records_untouched():
    # uvicorn's access formatter unpacks record.args; a clean record must keep them.
    args = ("127.0.0.1:1", "GET", "/health", "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d', args, None)
    assert RedactingFilter(URLS[0]).filter(record)
    assert record.args == args


def test_logging_filter_scrubs_message_args_and_traceback():
    url = URLS[0]
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Capture()
    handler.addFilter(RedactingFilter(url))
    logger = logging.getLogger("test_rpc_redaction")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("calling %s", url)
        try:
            raise ConnectionError(f"Max retries exceeded with url: /v2/{KEY}")
        except ConnectionError:
            logger.exception("boom")
    finally:
        logger.removeHandler(handler)
    assert records and all(KEY not in r for r in records), records
    assert "base-sepolia.g.alchemy.com" in records[0]
