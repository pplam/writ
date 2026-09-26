"""`writ serve`: the HTTP surface, the event stream, and the command.

The property most worth defending here is that nothing writes. A dashboard left
open on a live project is only safe if that is true by construction, so these
tests assert it about the routing table itself rather than about the routes the
UI happens to call.
"""

from __future__ import annotations

import inspect
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

import pytest

from tests.conftest import LEGACY_PLAN
from writ import config, server, state
from writ.cli import build_parser


@pytest.fixture()
def served(writ, design, project, capfd):
    """A live server on an ephemeral port, in a thread, torn down after.

    Built through `server._server_class()` rather than `ThreadingHTTPServer`
    directly, so tests exercise the error handling the real command installs.
    """
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    handler = type("Handler", (server._Handler,), {"root": project})
    httpd = server._server_class()(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield base, project
    finally:
        httpd.closing = True
        httpd.shutdown()
        httpd.server_close()


def get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.headers, response.read()


def read_events(url: str, *, count: int, timeout: float = 20.0) -> list[tuple[str, str]]:
    """Read `count` SSE events, returning (name, data) pairs."""
    events: list[tuple[str, str]] = []
    with urllib.request.urlopen(url, timeout=timeout) as response:
        name = ""
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                events.append((name, line[6:]))
                if len(events) >= count:
                    break
    return events


# ------------------------------------------------------------- the http surface


def test_the_page_is_served_at_the_root(served):
    base, _ = served
    status, headers, body = get(f"{base}/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<title>writ</title>" in body


def test_the_page_loads_the_compiled_assets(served):
    base, _ = served
    _, _, page = get(f"{base}/")
    assert b'src="app.js"' in page
    assert b'href="style.css"' in page
    for asset, kind in (("app.js", "text/javascript"), ("style.css", "text/css")):
        status, headers, body = get(f"{base}/{asset}")
        assert status == 200
        assert headers["Content-Type"].startswith(kind)
        assert body, f"{asset} is empty"


def test_the_snapshot_carries_every_view(served):
    base, _ = served
    _, headers, body = get(f"{base}/api/snapshot")
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    for view in (
        "overview", "tasks", "runs", "graph", "decisions", "phase"
    ):
        assert view in payload, view


def test_a_detail_route_answers_for_one_id(served):
    base, _ = served
    _, _, body = get(f"{base}/api/task/M01-001")
    assert json.loads(body)["id"] == "M01-001"


def test_a_run_id_with_its_slash_encoded_is_one_id(served):
    base, project = served
    with state.transaction(project) as data:
        data["runs"]["M01-001/01-implement"] = {
            "id": "M01-001/01-implement", "task": "M01-001", "role": "agent",
            "status": "completed", "dir": str(state.run_dir(project, "M01-001/01-implement")),
        }
    _, _, body = get(f"{base}/api/run/M01-001%2F01-implement")
    assert json.loads(body)["id"] == "M01-001/01-implement"


def test_an_unknown_id_is_a_404_with_a_readable_reason(served):
    base, _ = served
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(f"{base}/api/task/M99-999")
    assert caught.value.code == 404
    assert b"M99-999" in caught.value.read()


def test_a_planning_step_serves_its_own_output(served):
    """The step output route, including the colon its ids carry.

    `stage:requirements` and `critic:feasibility@r2` go through
    `encodeURIComponent` on the client, so the route has to unquote. A step id
    that is not on the record is a 404, which is what makes the path safe: it
    comes from the record, never from the request.
    """
    from writ import phases

    base, project = served
    phase_id = phases.begin(
        project,
        doc="design.md",
        plan_id="design-20250101T000000",
        steps=phases.declare(stages=(), synthesis=True),
    )
    phases.start_step(project, phase_id, "synthesis")
    _, _, body = get(f"{base}/api/phase/step/synthesis")
    assert json.loads(body)["step"] == "synthesis"

    _, _, body = get(f"{base}/api/phase")
    assert json.loads(body)["id"] == phase_id

    for attempt in ("stage%3Anope", "..%2F..%2Fetc%2Fpasswd", "nope"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(f"{base}/api/phase/step/{attempt}")
        assert caught.value.code == 404, attempt


def test_an_unknown_route_is_a_404(served):
    base, _ = served
    for path in ("/nope", "/api/nope", "/api/task", "/api"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(f"{base}{path}")
        assert caught.value.code == 404, path


# ------------------------------------------------------------------ read-only


def test_no_route_accepts_a_write_method(served):
    """Read-only by construction: the handler implements no write verb at all."""
    base, _ = served
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        request = urllib.request.Request(f"{base}/api/snapshot", method=method)
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 501, method


def test_the_handler_defines_only_a_get(served):
    """Stated against the class, so adding a writer cannot pass unnoticed."""
    verbs = {name for name in dir(server._Handler) if name.startswith("do_")}
    assert verbs == {"do_GET"}


def test_serving_a_project_does_not_modify_it(served):
    base, project = served
    path = state.state_file(project)
    before = path.read_bytes()
    get(f"{base}/api/snapshot")
    get(f"{base}/api/task/M01-001")
    get(f"{base}/api/graph")
    assert path.read_bytes() == before


def test_there_is_no_path_that_serves_an_arbitrary_file(served):
    """No traversal to defend against: assets are a fixed table, not a join."""
    base, _ = served
    for attempt in (
        "/../../../../etc/passwd",
        "/..%2f..%2fetc%2fpasswd",
        "/app.js/../../../etc/passwd",
        "/.writ/state.json",
    ):
        try:
            status, _, body = get(f"{base}{attempt}")
        except urllib.error.HTTPError as exc:
            assert exc.code in (400, 404), attempt
        else:
            assert b"root:" not in body, attempt
            assert status == 200 and attempt.startswith("/app.js")


def test_assets_come_from_the_package_not_the_project(served):
    """A project cannot shadow the dashboard's own script."""
    base, project = served
    (project / "app.js").write_text("alert('hijacked')")
    _, _, body = get(f"{base}/app.js")
    assert b"hijacked" not in body


# ------------------------------------------------------------- the live stream


def test_the_stream_sends_a_snapshot_immediately(served):
    base, _ = served
    events = read_events(f"{base}/events", count=1)
    assert events[0][0] == "snapshot"
    assert "overview" in json.loads(events[0][1])


def test_a_change_to_the_store_pushes_a_new_snapshot(served):
    """Any writer at all: the stream follows the file, not the orchestrator."""
    base, project = served

    def touch():
        threading.Event().wait(1.0)
        data = state.load(project)
        data["tasks"]["M01-001"]["title"] = "renamed by a raw edit"
        state.save(project, data)

    threading.Thread(target=touch, daemon=True).start()
    events = read_events(f"{base}/events", count=2)
    assert len(events) == 2
    latest = json.loads(events[-1][1])
    titles = [task["title"] for task in latest["tasks"]]
    assert "renamed by a raw edit" in titles


def test_an_idle_stream_still_says_it_is_alive(served, monkeypatch):
    base, _ = served
    monkeypatch.setattr(server, "HEARTBEAT_SECONDS", 0.5)
    names = [name for name, _ in read_events(f"{base}/events", count=2)]
    assert names[0] == "snapshot"
    assert names[1] == "ping"


def test_a_torn_read_does_not_kill_the_stream(served, monkeypatch):
    """A writer mid-rewrite must cost one tick, not the connection."""
    base, project = served
    calls = {"n": 0}
    real = server.api.everything

    def flaky(root):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("torn read")
        return real(root)

    monkeypatch.setattr(server.api, "everything", flaky)
    monkeypatch.setattr(server, "HEARTBEAT_SECONDS", 0.5)

    def touch():
        threading.Event().wait(0.8)
        data = state.load(project)
        data["tasks"]["M01-001"]["title"] = "after the torn read"
        state.save(project, data)

    threading.Thread(target=touch, daemon=True).start()
    names = [name for name, _ in read_events(f"{base}/events", count=2)]
    assert names[0] == "snapshot"


# ---------------------------------------------------------------- the command


def test_serve_is_a_top_level_command():
    """Not a flag on graph: it covers runs, prompts and logs, not just the DAG."""
    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    assert "serve" in action.choices


def test_graph_no_longer_carries_a_serve_flag():
    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    flags = {
        option
        for sub in action.choices["graph"]._actions
        for option in sub.option_strings
    }
    assert "--serve" not in flags


def test_serve_binds_this_machine_only_by_default(project):
    """Loopback unless a project says otherwise, since the page has no auth.

    Checked after the config is applied rather than straight off the parser: the
    flag parses as None so that `serve.host` in a config can be overridden by it,
    which means the default now lives in `config.DEFAULTS` and the parser alone no
    longer knows it.
    """
    args = build_parser().parse_args(["--root", str(project), "serve"])
    config.apply(args, config.load(project))
    assert args.host == server.DEFAULT_HOST == "127.0.0.1"
    assert args.port == server.DEFAULT_PORT


def test_a_taken_port_is_a_message_not_a_traceback(writ, design, project, monkeypatch):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)

    class _Fake:
        def __init__(self, *args, **kwargs):
            raise OSError(48, "Address already in use")

    monkeypatch.setattr(server, "ThreadingHTTPServer", _Fake)
    with pytest.raises(state.WritError) as caught:
        server.serve(project, port=8731, open_browser=False)
    assert "cannot serve on 127.0.0.1:8731" in str(caught.value)
    assert "--port 8732" in str(caught.value)


def test_a_bad_host_does_not_suggest_another_port(writ, design, project, monkeypatch):
    """Confident, useless advice is worse than none."""
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)

    class _Fake:
        def __init__(self, *args, **kwargs):
            raise OSError(8, "nodename nor servname provided")

    monkeypatch.setattr(server, "ThreadingHTTPServer", _Fake)
    with pytest.raises(state.WritError) as caught:
        server.serve(project, host="not-a-host", open_browser=False)
    assert "--port" not in str(caught.value)


def test_binding_beyond_loopback_warns_that_there_is_no_auth(
    writ, design, project, monkeypatch, capsys
):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)

    class _Fake:
        server_port = 8731

        def __init__(self, *args, **kwargs):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(server, "ThreadingHTTPServer", _Fake)
    server.serve(project, host="0.0.0.0", open_browser=False)
    err = capsys.readouterr().err
    assert "no authentication" in err


def test_serving_an_uninitialised_project_fails_at_the_prompt(tmp_path):
    """Better a message now than a page that renders an error per panel."""
    with pytest.raises(state.WritError):
        server.serve(tmp_path / "nothing", open_browser=False)


# ------------------------------------------------------- clients that hang up


def reset(base: str, request: bytes, *, read: bool, settle: float) -> None:
    """Send `request`, then abort the connection with an RST rather than a FIN.

    SO_LINGER with a zero timeout is what turns close() into a reset, which is
    what a browser reaping an idle keep-alive connection actually does, and what
    a clean shutdown would not reproduce.
    """
    port = int(base.rsplit(":", 1)[1])
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.sendall(request)
        if read:
            sock.recv(64)
        time.sleep(settle)
    finally:
        sock.close()
    time.sleep(settle)


@pytest.mark.parametrize(
    "request_bytes, read, description",
    [
        (b"GET /api/snapshot HTTP/1.1\r\nHost: x\r\n\r\n", False, "idle keep-alive"),
        (b"GET /app.js HTTP/1.1\r\nHost: x\r\n\r\n", True, "mid-body"),
        (b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n", False, "open event stream"),
        (b"", False, "connected and said nothing"),
    ],
)
def test_a_client_hanging_up_is_not_reported_as_a_crash(
    served, capfd, request_bytes, read, description
):
    """A disconnect prints nothing.

    Because `protocol_version` is HTTP/1.1 the connection is kept alive and the
    thread parks in `readline()` waiting for another request. Browsers reap idle
    connections constantly and some do it with an RST, which `socketserver` sends
    to `handle_error` — so simply leaving the page open produced a twenty-line
    traceback every few seconds about something that had gone entirely right.

    Four shapes, because the first fix only covered the first one: between
    requests, part-way through a body, during a held-open stream, and a client
    that connects and says nothing at all.
    """
    base, _ = served
    capfd.readouterr()  # discard anything from setup
    reset(base, request_bytes, read=read, settle=0.35)
    captured = capfd.readouterr()
    assert "Traceback" not in captured.err, f"{description}: {captured.err}"
    assert "ConnectionResetError" not in captured.err


def test_the_server_still_answers_after_a_client_aborts(served, capfd):
    """A reset connection does not take the server with it."""
    base, _ = served
    reset(base, b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n", read=False, settle=0.35)
    status, _, body = get(f"{base}/api/snapshot")
    assert status == 200
    assert json.loads(body)["tasks"]


def test_a_real_fault_is_still_reported_in_full(served, capfd, monkeypatch):
    """Only disconnects are silenced.

    The risk in quieting `handle_error` is quieting everything, so this asserts
    the opposite case directly: an ordinary exception in a handler still gets its
    traceback and its name. A silent 500 would be far worse than the noise.
    """
    base, _ = served

    def broken(root):
        raise RuntimeError("a real bug, not a disconnect")

    monkeypatch.setattr(server.api, "overview", broken)
    capfd.readouterr()
    with pytest.raises(urllib.error.HTTPError):
        get(f"{base}/api/overview", timeout=5.0)
    time.sleep(0.3)
    captured = capfd.readouterr()
    assert "Traceback" in captured.err
    assert "a real bug, not a disconnect" in captured.err


def test_the_disconnect_check_names_the_errors_rather_than_catching_everything():
    """`handle_error` filters on exception type, not on a bare except.

    Recorded as a test because "stop printing tracebacks" has an easy wrong
    implementation that also hides genuine failures, and the difference is one
    line in a place nobody looks twice at.
    """
    source = inspect.getsource(server._server_class)
    assert "ConnectionError" in source
    assert "TimeoutError" in source
    assert "except Exception" not in source
    assert "except:" not in source


def test_an_unexpected_fault_answers_with_a_500_rather_than_dropping_the_socket(
    served, capfd, monkeypatch
):
    """A bug inside a handler is still an HTTP response.

    Found by writing the test above: an unexpected exception escaped `do_GET`
    with no status line sent, so the connection simply closed. In the browser
    that is a bare network error — identical to writ having been stopped — which
    points the reader at the wrong problem entirely.
    """
    base, _ = served

    def broken(root):
        raise RuntimeError("a real bug, not a disconnect")

    monkeypatch.setattr(server.api, "overview", broken)
    capfd.readouterr()
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(f"{base}/api/overview", timeout=5.0)
    assert caught.value.code == 500
    assert "internal error" in json.loads(caught.value.read())["error"]
    time.sleep(0.3)
    assert "a real bug, not a disconnect" in capfd.readouterr().err
