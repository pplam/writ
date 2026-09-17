"""The dashboard: layout, the snapshot it serves, and the live stream.

The HTTP surface is exercised against a real server on a real socket rather than
by calling handler methods, because the things worth testing here are wire
behaviour: status codes, content types, and whether an event actually arrives on
a stream while a run is changing the store.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPConnection

import pytest

from writ import dashboard, state
from writ.state import WritError


@pytest.fixture
def served(planned, writ, project):
    """A dashboard on an ephemeral port, shut down when the test ends."""
    handler = type("Handler", (dashboard._Handler,), {"root": project})
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.closing = False
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.closing = True
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(url: str) -> tuple[int, str, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.headers["Content-Type"], response.read().decode()


# --------------------------------------------------------------------------
# the snapshot


def test_the_snapshot_places_every_task(planned, project):
    snap = dashboard.snapshot(project)
    data = state.load(project)
    assert len(snap["nodes"]) == len(data["tasks"])


def test_columns_follow_dependency_depth(planned, writ, project):
    """A column is work that could run at once, which is what depth means."""
    snap = dashboard.snapshot(project)
    columns = {node["id"]: node["column"] for node in snap["nodes"]}
    tasks = state.load(project)["tasks"]
    for task_id, task in tasks.items():
        for dep in task.get("depends_on", []):
            assert columns[dep] < columns[task_id], f"{dep} not left of {task_id}"


def test_no_two_tasks_land_on_the_same_spot(planned, writ, project):
    writ("task", "--title", "Extra", "--milestone", "M01", "--acceptance", "a")
    snap = dashboard.snapshot(project)
    spots = [(node["x"], node["y"]) for node in snap["nodes"]]
    assert len(spots) == len(set(spots))


def test_every_edge_points_forwards(planned, project):
    """A dependency is always in an earlier column, so no edge doubles back."""
    snap = dashboard.snapshot(project)
    assert snap["edges"]
    for edge in snap["edges"]:
        assert edge["x1"] < edge["x2"], edge


def test_an_edge_is_satisfied_once_its_source_completes(planned, writ, project):
    before = dashboard.snapshot(project)
    assert not any(edge["satisfied"] for edge in before["edges"])
    writ("override", "M01-001", "completed", "--reason", "done by hand")
    after = dashboard.snapshot(project)
    from_root = [e for e in after["edges"] if e["from"] == "M01-001"]
    assert from_root and all(edge["satisfied"] for edge in from_root)


def test_the_snapshot_carries_status_and_acceptance(planned, project):
    snap = dashboard.snapshot(project)
    node = next(n for n in snap["nodes"] if n["id"] == "M01-001")
    assert node["status"] == "ready"
    assert node["total"] == 3  # M01-001 has three criteria
    assert node["passed"] == 0


def test_ready_is_derived_not_stored(planned, writ, project):
    """The page must show the same `ready` the scheduler acts on."""
    snap = dashboard.snapshot(project)
    statuses = {node["id"]: node["status"] for node in snap["nodes"]}
    assert statuses["M01-001"] == "ready"
    assert statuses["M02-001"] == "planned"
    writ("override", "M01-001", "completed", "--reason", "x")
    statuses = {n["id"]: n["status"] for n in dashboard.snapshot(project)["nodes"]}
    assert statuses["M02-001"] == "ready"


def test_the_snapshot_reports_reverse_edges(planned, project):
    snap = dashboard.snapshot(project)
    node = next(n for n in snap["nodes"] if n["id"] == "M01-001")
    assert node["blocks"] == ["M02-001"]


def test_totals_count_live_work(planned, writ, project):
    assert dashboard.snapshot(project)["totals"]["live"] == 0
    writ("set", "M01-001", "running", "--force")
    assert dashboard.snapshot(project)["totals"]["live"] == 1


def test_milestones_carry_their_progress(planned, writ, project):
    writ("override", "M01-001", "completed", "--reason", "x")
    snap = dashboard.snapshot(project)
    first = next(m for m in snap["milestones"] if m["id"] == "M01")
    assert (first["done"], first["total"]) == (1, 1)


def test_proposed_decisions_are_surfaced(planned, writ, project):
    """They need a human, so a dashboard that hid them would hide the blocker."""
    assert dashboard.snapshot(project)["decisions"] == []
    data = state.load(project)
    data["decisions"].append(
        {"id": "D-0001", "title": "Frames are length-prefixed",
         "status": "proposed", "by": "agent(claude)"}
    )
    state.save(project, data)
    surfaced = dashboard.snapshot(project)["decisions"]
    assert [d["id"] for d in surfaced] == ["D-0001"]


def test_confirmed_decisions_are_not_surfaced(planned, project):
    data = state.load(project)
    data["decisions"].append(
        {"id": "D-0001", "title": "settled", "status": "active", "by": "operator"}
    )
    state.save(project, data)
    assert dashboard.snapshot(project)["decisions"] == []


def test_the_canvas_grows_to_fit(planned, writ, project):
    small = dashboard.snapshot(project)
    for index in range(6):
        writ("task", "--title", f"Wide {index}", "--milestone", "M01",
             "--acceptance", "a")
    large = dashboard.snapshot(project)
    assert large["height"] > small["height"]


def test_an_empty_project_still_produces_a_snapshot(writ, project):
    writ("init")
    snap = dashboard.snapshot(project)
    assert snap["nodes"] == [] and snap["edges"] == []
    assert snap["totals"]["tasks"] == 0


def test_a_dangling_dependency_is_an_error_not_a_blank_node(planned, project):
    """A store that names a task which does not exist is corrupt.

    Everywhere else in writ that is an error, and the dashboard agrees rather
    than inventing a lenient reading of a broken graph. The layout still filters
    unknown ids out of its geometry, so nothing tries to draw an edge to a node
    that was never placed.
    """
    data = state.load(project)
    data["tasks"]["M01-001"]["depends_on"] = ["M99-999"]
    state.save(project, data)
    with pytest.raises(WritError, match="unknown task M99-999"):
        dashboard.snapshot(project)


# --------------------------------------------------------------------------
# the http surface


def test_the_page_is_served(served):
    status, content_type, body = get(served + "/")
    assert status == 200
    assert "text/html" in content_type
    assert "<svg" in body or "createElementNS" in body


def test_the_page_needs_no_network_to_render(served):
    """No CDN, no bundle: the page is one file and works offline.

    The one http:// left is the SVG namespace, which is an identifier the spec
    fixes rather than an address anything fetches.
    """
    _, _, body = get(served + "/")
    fetchable = body.replace("http://www.w3.org/2000/svg", "")
    assert "http://" not in fetchable
    assert "https://" not in fetchable
    assert "<script src" not in body
    assert "<link" not in body


def test_the_api_returns_the_snapshot(served, project):
    status, content_type, body = get(served + "/api/graph")
    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body)["nodes"]


def test_an_unknown_path_is_a_404(served):
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(served + "/secrets")
    assert caught.value.code == 404


def test_a_trailing_slash_is_the_same_page(served):
    assert get(served + "/api/graph/")[0] == 200


def test_nothing_is_cached(served):
    """A stale graph is worse than a slow one."""
    with urllib.request.urlopen(served + "/api/graph", timeout=5) as response:
        assert response.headers["Cache-Control"] == "no-store"


def test_the_api_reflects_a_change_without_a_restart(served, writ):
    first = json.loads(get(served + "/api/graph")[2])
    writ("override", "M01-001", "completed", "--reason", "x")
    second = json.loads(get(served + "/api/graph")[2])
    assert first["totals"].get("completed", 0) == 0
    assert second["totals"]["completed"] == 1


def test_the_server_holds_no_state_between_requests(served, writ, project):
    """The store is the state; the server is a window onto it."""
    writ("override", "M01-001", "completed", "--reason", "x")
    body = json.loads(get(served + "/api/graph")[2])
    node = next(n for n in body["nodes"] if n["id"] == "M01-001")
    assert node["status"] == "completed"


# --------------------------------------------------------------------------
# the live stream


def read_events(url: str, *, count: int, timeout: float = 15.0) -> list[dict]:
    """Collect `count` SSE payloads, or fewer if the timeout runs out."""
    parsed = urllib.parse.urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    connection.request("GET", parsed.path)
    response = connection.getresponse()
    events: list[dict] = []
    buffer = b""
    deadline = time.monotonic() + timeout
    try:
        while len(events) < count and time.monotonic() < deadline:
            chunk = response.read(1)
            if not chunk:
                break
            buffer += chunk
            if buffer.endswith(b"\n\n"):
                text = buffer.decode()
                match = re.search(r"event: (\w+)\ndata: (.*)", text, re.S)
                if match:
                    events.append(
                        {"event": match.group(1), "data": json.loads(match.group(2))}
                    )
                buffer = b""
    finally:
        connection.close()
    return events



def test_the_stream_sends_the_graph_immediately(served):
    """A tab that opened mid-run should not wait for the next change."""
    events = read_events(served + "/events", count=1)
    assert events and events[0]["event"] == "graph"
    assert events[0]["data"]["nodes"]


def test_the_stream_announces_itself_as_events(served):
    parsed = urllib.parse.urlsplit(served)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.request("GET", "/events")
    response = connection.getresponse()
    try:
        assert response.headers["Content-Type"] == "text/event-stream"
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        connection.close()


def test_a_change_pushes_a_new_snapshot(served, writ):
    """The whole point: the page follows the store without being asked."""
    result: list[list[dict]] = []
    reader = threading.Thread(
        target=lambda: result.append(read_events(served + "/events", count=2))
    )
    reader.start()
    time.sleep(dashboard.POLL_SECONDS * 2)
    writ("override", "M01-001", "completed", "--reason", "pushed")
    reader.join(timeout=20)
    assert result and len(result[0]) == 2
    statuses = [
        {n["id"]: n["status"] for n in event["data"]["nodes"]}["M01-001"]
        for event in result[0]
    ]
    assert statuses[0] == "ready"
    assert statuses[-1] == "completed"


def test_the_stream_watches_the_file_not_the_writer(served, writ):
    """Any writer shows up: a hand-run command, not just `writ run`."""
    result: list[list[dict]] = []
    reader = threading.Thread(
        target=lambda: result.append(read_events(served + "/events", count=2))
    )
    reader.start()
    time.sleep(dashboard.POLL_SECONDS * 2)
    writ("task", "--title", "Added by hand", "--milestone", "M01",
         "--acceptance", "a")
    reader.join(timeout=20)
    assert result and len(result[0]) == 2
    assert len(result[0][-1]["data"]["nodes"]) > len(result[0][0]["data"]["nodes"])


def test_a_closed_tab_does_not_take_the_server_down(served, writ):
    """A dropped stream is normal; it must not raise into the server thread."""
    parsed = urllib.parse.urlsplit(served)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.request("GET", "/events")
    connection.getresponse().read(1)
    connection.close()  # walk away mid-stream
    writ("override", "M01-001", "completed", "--reason", "x")
    time.sleep(dashboard.POLL_SECONDS * 3)
    assert get(served + "/api/graph")[0] == 200


def test_many_tabs_are_served_at_once(served):
    """Threaded, so one open stream cannot block the next request."""
    streams = []
    try:
        for _ in range(3):
            parsed = urllib.parse.urlsplit(served)
            connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            connection.request("GET", "/events")
            connection.getresponse().read(1)
            streams.append(connection)
        assert get(served + "/api/graph")[0] == 200
    finally:
        for connection in streams:
            connection.close()


# --------------------------------------------------------------------------
# the command


def graph_help() -> str:
    """`graph --help`, taken from the parser rather than through the harness.

    The harness redirects stdout itself and argparse raises SystemExit straight
    through it, so the captured text never comes back. Formatting the subparser
    directly asks the same question without the fight.
    """
    from writ.cli import build_parser

    actions = [
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    return actions[0].choices["graph"].format_help()


def test_serve_is_a_way_of_drawing_the_graph():
    """It lives on `graph` because it answers the same question."""
    text = graph_help()
    assert "--serve" in text
    assert "--port" in text


def test_the_dashboard_binds_to_this_machine_only():
    """A default of 0.0.0.0 would publish an unauthenticated page to the LAN."""
    assert "127.0.0.1" in graph_help()
    import inspect

    assert inspect.signature(dashboard.serve).parameters["host"].default == "127.0.0.1"


def test_a_cyclic_graph_is_refused_before_serving(planned, writ, project):
    """Otherwise the failure arrives as a broken page instead of a message."""
    data = state.load(project)
    data["tasks"]["M01-001"]["depends_on"] = ["M03-001"]
    state.save(project, data)
    code, _, err = writ("graph", "--serve", "--no-open")
    assert code != 0
    assert "cycle" in err.lower()


def test_a_taken_port_is_a_message_not_a_traceback(planned, project):
    handler = type("Handler", (dashboard._Handler,), {"root": project})
    from http.server import ThreadingHTTPServer

    holder = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    try:
        with pytest.raises(WritError) as caught:
            dashboard.serve(
                project, port=holder.server_port, open_browser=False
            )
        assert "cannot serve" in str(caught.value)
        assert f"--port {holder.server_port + 1}" in str(caught.value)
    finally:
        holder.server_close()


def test_an_unusable_host_does_not_suggest_another_port(planned, project):
    """`--port N+1` would be confident and useless advice for a bad address."""
    with pytest.raises(WritError) as caught:
        dashboard.serve(project, host="10.99.99.99", open_browser=False)
    assert "cannot serve" in str(caught.value)
    assert "--port" not in str(caught.value)


def test_serving_off_loopback_warns(planned, project, capsys, monkeypatch):
    """The page has no auth, so a non-local bind publishes the project."""
    started: list[str] = []

    class _Fake:
        server_port = 9999

        def __init__(self, address, handler):
            started.append(address[0])

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", _Fake)
    dashboard.serve(project, host="0.0.0.0", open_browser=False)
    assert "no authentication" in capsys.readouterr().err
    assert started == ["0.0.0.0"]


def test_serving_on_loopback_does_not_warn(planned, project, capsys, monkeypatch):
    class _Fake:
        server_port = 9999

        def __init__(self, address, handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", _Fake)
    dashboard.serve(project, open_browser=False)
    captured = capsys.readouterr()
    assert "warning" not in captured.err
    assert "http://localhost:9999/" in captured.out
