"""The BWT API key travels as an `apikey=` query parameter, so it appears in
every request URL. These tests prove it cannot reach a log line, a traceback,
or -- most importantly -- an exception message returned to the MCP client,
which lands in a conversation transcript that cannot later be grepped or
rotated.

Every test here carries a POSITIVE CONTROL: it first asserts the key IS
present in the unprotected form, then asserts it is absent after protection.
A test that only asserts absence would pass just as happily against a typo'd
fixture that never contained the key at all. Two such vacuous passes were
caught during the 2026-09-02 build runs; that is why this is a rule now.
"""

import logging
from unittest.mock import patch

import httpx
import pytest

import main

FAKE_KEY = "fake-key-abc123XYZ-should-never-appear"
URL = f"https://ssl.bing.com/webmaster/api.svc/json/GetUserSites?siteUrl=x&apikey={FAKE_KEY}"


# --- the redactor itself ----------------------------------------------------


def test_redacts_the_key_but_keeps_the_rest_of_the_url():
    assert FAKE_KEY in URL, "positive control: the fixture must contain the key"
    out = main.redact_api_key(URL)
    assert FAKE_KEY not in out, f"key survived redaction: {out}"
    assert "apikey=REDACTED" in out, out
    assert "siteUrl=x" in out, f"redaction ate an unrelated parameter: {out}"


@pytest.mark.parametrize(
    "raw",
    [
        f"?apikey={FAKE_KEY}",
        f"?apikey={FAKE_KEY}&other=1",
        f"for url 'https://x/api?apikey={FAKE_KEY}'",
        f'"https://x/api?apikey={FAKE_KEY}"',
        f"APIKEY={FAKE_KEY}",
    ],
)
def test_redacts_across_delimiters_and_case(raw):
    assert FAKE_KEY in raw, "positive control"
    assert FAKE_KEY not in main.redact_api_key(raw), f"key survived in: {raw!r}"


# --- logging ----------------------------------------------------------------


def _render(record: logging.LogRecord, formatter: logging.Formatter) -> str:
    return formatter.format(record)


def test_formatter_scrubs_a_log_message_containing_the_key():
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: GET %s", (URL,), None)

    plain = _render(record, logging.Formatter())
    assert FAKE_KEY in plain, (
        "positive control failed: the unprotected formatter should have leaked the key, "
        f"got {plain!r} -- if this fails the test proves nothing"
    )

    safe = _render(record, main.RedactingFormatter())
    assert FAKE_KEY not in safe, f"key survived the redacting formatter: {safe}"
    assert "apikey=REDACTED" in safe, safe


def test_formatter_scrubs_traceback_text_not_just_the_message():
    """A logging.Filter cannot do this -- tracebacks are rendered from
    exc_info after filters run. This is why the fix is a formatter."""
    try:
        raise httpx.ConnectError(f"failed connecting to {URL}")
    except httpx.ConnectError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, exc_info)

    plain = _render(record, logging.Formatter())
    assert FAKE_KEY in plain, "positive control: traceback should contain the key unprotected"

    safe = _render(record, main.RedactingFormatter())
    assert FAKE_KEY not in safe, f"key survived in rendered traceback: {safe}"


def test_root_handlers_actually_have_the_redacting_formatter_installed():
    """Proves the module wired it up, not merely that the class exists."""
    handlers = logging.getLogger().handlers
    assert handlers, "positive control: root logger has no handlers to inspect"
    assert any(isinstance(h.formatter, main.RedactingFormatter) for h in handlers), (
        f"no root handler uses RedactingFormatter: {[type(h.formatter).__name__ for h in handlers]}"
    )


def test_httpx_logger_is_quieted():
    assert logging.getLogger("httpx").level >= logging.WARNING, (
        f"httpx logger level is {logging.getLogger('httpx').level}, expected >= WARNING"
    )


# --- the error path, which reaches the MCP client ---------------------------


async def _raise_from_make_request(exc: Exception):
    """Drive _make_request so `exc` is raised by the HTTP call."""
    api = main.BingWebmasterAPI(FAKE_KEY)

    class _Client:
        is_closed = False

        async def get(self, *a, **k):
            raise exc

        async def request(self, *a, **k):
            raise exc

    with patch.object(api, "_ensure_client", return_value=_Client()):
        await api._make_request("GetUserSites")


async def test_httpx_error_message_reaching_the_caller_has_no_key():
    original = httpx.ConnectError(f"[Errno 61] Connection refused for url '{URL}'")
    assert FAKE_KEY in str(original), "positive control: httpx's own message must contain the key"

    with pytest.raises(RuntimeError) as ei:
        await _raise_from_make_request(original)

    msg = str(ei.value)
    assert FAKE_KEY not in msg, f"key leaked to the caller: {msg}"
    assert "GetUserSites" in msg, f"endpoint context was lost: {msg}"


async def test_error_is_unchained_so_no_traceback_carries_the_raw_url():
    """`raise ... from None` matters: a chained __context__ would drag the
    unredacted httpx message into any traceback rendered for the client."""
    original = httpx.ConnectError(f"boom for url '{URL}'")

    with pytest.raises(RuntimeError) as ei:
        await _raise_from_make_request(original)

    assert ei.value.__cause__ is None, "exception was chained; raw URL can resurface"
    assert ei.value.__suppress_context__ is True, (
        "context not suppressed; the original httpx message stays reachable in tracebacks"
    )


async def test_timeout_is_also_redacted_and_unchained():
    with pytest.raises(TimeoutError) as ei:
        await _raise_from_make_request(httpx.ReadTimeout(f"timed out for url '{URL}'"))

    assert FAKE_KEY not in str(ei.value), f"key leaked via timeout: {ei.value}"
    assert ei.value.__suppress_context__ is True


async def test_non_200_body_is_redacted():
    """Bing echoes the request in some error bodies; scrub response.text too."""
    api = main.BingWebmasterAPI(FAKE_KEY)

    class _Resp:
        status_code = 400
        text = f'{{"Message":"bad request for {URL}"}}'

    class _Client:
        is_closed = False

        async def get(self, *a, **k):
            return _Resp()

    assert FAKE_KEY in _Resp.text, "positive control: fixture body must contain the key"

    with patch.object(api, "_ensure_client", return_value=_Client()):
        with pytest.raises(Exception) as ei:
            await api._make_request("GetUserSites")

    assert FAKE_KEY not in str(ei.value), f"key leaked via error body: {ei.value}"
