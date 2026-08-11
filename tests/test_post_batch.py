"""Offline tests for testray_writer.post_batch — the HTTP upsert path.

Runs a throwaway HTTP server on localhost that impersonates the bits of Testray
we touch (`/o/oauth2/token` + `/o/c/triageresults/by-external-reference-code/*`),
so the verb, path, headers, payload, 401-remint, no-retry-on-4xx and
retry-on-5xx behavior are all checked without a DXP.

Closes the gap noted in tests/TESTING.md ("no automated test yet for
post_batch's HTTP path").
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from testray_analytics.analysis import testray_writer as tw

# Per-ERC canned responses, set by each test: erc -> [status, status, ...]
# consumed one per attempt; a missing entry means 200.
RESPONSES: dict = {}
CALLS: list = []      # every non-token request: (method, path, headers, body)
TOKEN_CALLS: list = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # keep pytest output clean
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, payload=b'{}'):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path == "/o/oauth2/token":
            TOKEN_CALLS.append(self._read_body().decode())
            self._send(200, json.dumps(
                {"access_token": f"tok-{len(TOKEN_CALLS)}"}).encode())
            return
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def _handle(self, method):
        body = self._read_body()
        CALLS.append((method, self.path, dict(self.headers),
                      json.loads(body) if body else None))
        erc = urllib.parse.unquote(self.path.rsplit("/", 1)[-1])
        queue = RESPONSES.get(erc)
        code = queue.pop(0) if queue else 200
        if code == "CLOSE":            # hang up without answering
            self.close_connection = True
            return
        if code == 200:
            self._send(200, json.dumps({"id": 1}).encode())
        else:
            self._send(code, json.dumps({"title": f"boom {code}"}).encode())


@pytest.fixture
def server():
    RESPONSES.clear(); CALLS.clear(); TOKEN_CALLS.clear()
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {
        "base_url": f"http://127.0.0.1:{srv.server_port}",
        "client_id": "cid", "client_secret": "sec",
    }
    srv.shutdown()


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(tw.time, "sleep", lambda *_: None)


def _item(erc, **kw):
    it = {"externalReferenceCode": erc, "classification": {"key": "BUG"},
          "confidence": {"key": "high"}, "classifier": "test"}
    it.update(kw)
    return it


# --- happy path -------------------------------------------------------------

def test_upserts_each_item_by_erc(server):
    items = [_item("b_1_c"), _item("b_2_c"), _item("b_3_c")]
    assert tw.post_batch(items, server) == (3, 0, [])
    assert [c[0] for c in CALLS] == ["PUT"] * 3
    assert [c[1] for c in CALLS] == [
        f"/o/c/triageresults/by-external-reference-code/{e}"
        for e in ("b_1_c", "b_2_c", "b_3_c")
    ]


def test_sends_auth_and_json_body(server):
    tw.post_batch([_item("b_1_c", culpritFile="x/Foo.java")], server)
    _, _, headers, body = CALLS[0]
    assert headers["Authorization"] == "Bearer tok-1"
    assert headers["Content-Type"] == "application/json"
    assert body["culpritFile"] == "x/Foo.java"
    assert body["classification"] == {"key": "BUG"}


def test_erc_is_url_quoted(server):
    # Real ERCs carry a classifier like "api:claude-opus-4-8".
    tw.post_batch([_item("505504995_99708_api:claude-opus-4-8")], server)
    assert CALLS[0][1].endswith("505504995_99708_api%3Aclaude-opus-4-8")


def test_empty_batch_makes_no_requests(server):
    assert tw.post_batch([], server) == (0, 0, [])
    assert CALLS == [] and TOKEN_CALLS == []


def test_token_minted_once_for_the_whole_batch(server):
    tw.post_batch([_item(f"b_{i}_c") for i in range(5)], server)
    assert len(TOKEN_CALLS) == 1


# --- failure handling -------------------------------------------------------

def test_401_remints_token_and_retries(server):
    RESPONSES["b_1_c"] = [401]
    assert tw.post_batch([_item("b_1_c")], server) == (1, 0, [])
    assert len(TOKEN_CALLS) == 2                      # initial + re-mint
    assert CALLS[1][2]["Authorization"] == "Bearer tok-2"


def test_4xx_is_not_retried_and_is_reported(server):
    RESPONSES["b_1_c"] = [400, 400, 400]
    n_ok, n_fail, failures = tw.post_batch([_item("b_1_c")], server)
    assert (n_ok, n_fail) == (0, 1)
    assert len(CALLS) == 1                            # no retry on a bad payload
    assert failures[0]["externalReferenceCode"] == "b_1_c"
    assert failures[0]["status"] == 400
    assert "boom 400" in failures[0]["error"]


def test_5xx_is_retried_then_reported(server):
    RESPONSES["b_1_c"] = [500, 500, 500]
    n_ok, n_fail, failures = tw.post_batch([_item("b_1_c")], server, max_retries=2)
    assert (n_ok, n_fail) == (0, 1)
    assert len(CALLS) == 3                            # initial + 2 retries
    assert failures[0]["status"] == 500


def test_5xx_that_recovers_counts_as_ok(server):
    RESPONSES["b_1_c"] = [503]
    assert tw.post_batch([_item("b_1_c")], server, max_retries=2) == (1, 0, [])


def test_one_bad_item_does_not_abort_the_batch(server):
    RESPONSES["b_2_c"] = [400]
    items = [_item("b_1_c"), _item("b_2_c"), _item("b_3_c")]
    n_ok, n_fail, failures = tw.post_batch(items, server)
    assert (n_ok, n_fail) == (2, 1)
    assert [f["externalReferenceCode"] for f in failures] == ["b_2_c"]


def test_dropped_connection_reported_with_null_status(server):
    RESPONSES["b_1_c"] = ["CLOSE", "CLOSE"]
    n_ok, n_fail, failures = tw.post_batch([_item("b_1_c")], server, max_retries=1)
    assert (n_ok, n_fail) == (0, 1)
    assert failures[0]["status"] is None          # transport, not HTTP
    assert failures[0]["error"]


def test_unreachable_host_fails_fast_at_token_mint(server):
    dead = dict(server, base_url="http://127.0.0.1:1")   # nothing listening
    with pytest.raises(Exception):
        tw.post_batch([_item("b_1_c")], dead)
    assert CALLS == []                            # nothing half-written
