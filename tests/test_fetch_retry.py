"""A dropped connection must not throw away a multi-page fetch.

A 40k-row build is ~84 sequential pages over several minutes. Prod dropped one
mid-fetch on 2026-08-24 and the whole prepare died with RemoteDisconnected
after ~4 minutes of paging.
"""
import http.client
import json
import urllib.error
from unittest import mock

import pytest

from testray_analytics.analysis.prepare import _testray_fetch_paginated


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _page(n_items=1, last_page=1):
    return _Resp({"items": [{"id": i} for i in range(n_items)],
                  "lastPage": last_page})


def _run(side_effect):
    with mock.patch("urllib.request.urlopen", side_effect=side_effect) as m, \
         mock.patch("time.sleep"):
        items = _testray_fetch_paginated("/o/c/x", {}, token="t",
                                         base_url="https://example.test")
    return items, m


def test_transient_disconnect_is_retried_and_the_fetch_completes():
    err = http.client.RemoteDisconnected("closed")
    items, m = _run([err, _page(3)])
    assert len(items) == 3
    assert m.call_count == 2


@pytest.mark.parametrize("err", [
    http.client.RemoteDisconnected("closed"),
    urllib.error.URLError("connection reset"),
    ConnectionResetError("reset"),
    TimeoutError("timed out"),
])
def test_each_connection_level_fault_is_retried(err):
    items, _ = _run([err, _page(1)])
    assert len(items) == 1


def test_it_gives_up_rather_than_retrying_forever():
    err = http.client.RemoteDisconnected("closed")
    with mock.patch("urllib.request.urlopen", side_effect=[err] * 12) as m, \
         mock.patch("time.sleep"):
        with pytest.raises(http.client.HTTPException):
            _testray_fetch_paginated("/o/c/x", {}, token="t",
                                     base_url="https://example.test")
    assert m.call_count == 4


def test_an_http_error_is_not_retried():
    """A 400 is an answer, not a fault — retrying asks the same bad question."""
    err = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
    with mock.patch("urllib.request.urlopen", side_effect=[err, _page(1)]) as m, \
         mock.patch("time.sleep"):
        with pytest.raises(urllib.error.HTTPError):
            _testray_fetch_paginated("/o/c/x", {}, token="t",
                                     base_url="https://example.test")
    assert m.call_count == 1


def test_a_401_still_fails_fast_with_its_own_message():
    err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    with mock.patch("urllib.request.urlopen", side_effect=[err]), \
         mock.patch("time.sleep"):
        with pytest.raises(SystemExit, match="401"):
            _testray_fetch_paginated("/o/c/x", {}, token="t",
                                     base_url="https://example.test")
