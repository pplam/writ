"""`writ serve`: the HTTP surface, the event stream, and the command.

The property most worth defending here is that nothing writes. A dashboard left
open on a live project is only safe if that is true by construction, so these
tests assert it about the routing table itself rather than about the routes the
UI happens to call.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from writ import server, state
from writ.cli import build_parser


@pytest.fixture()
def served(writ, design, project):
    """A live server on an ephemeral port, in a thread, torn down after."""
    writ("init")
    writ("plan", str(design), "--extract")
    handler = type("Handler", (server._Handler,), {"root": project})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.closing = False
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
    for view in ("overview", "tasks", "runs", "milestones", "graph", "decisions"):
        assert view in payload, view


def test_a_detail_route_answers_for_one_id(served):
    base, _ = served
    _, _, body = get(f"{base}/api/task/M01-001")
    assert json.loads(body)["id"] == "M01-001"


def test_an_unknown_id_is_a_404_with_a_readable_reason(served):
    base, _ = served
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(f"{base}/api/task/M99-999")
    assert caught.value.code == 404
    assert b"M99-999" in caught.value.read()


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


def test_serve_binds_this_machine_only_by_default():
    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    args = action.choices["serve"].parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == server.DEFAULT_PORT


def test_a_taken_port_is_a_message_not_a_traceback(writ, design, project, monkeypatch):
    writ("init")
    writ("plan", str(design), "--extract")

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
    writ("plan", str(design), "--extract")

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
    writ("plan", str(design), "--extract")

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
