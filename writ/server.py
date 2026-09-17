"""`writ serve`: a read-only web view of everything writ knows.

The terminal answers one question per command. That is right for driving work and
wrong for inspecting it: during a long run the questions come faster than you can
type them, and they are about relationships — which task is this run for, what was
that agent actually told, which criterion did the reviewer reject.

So this serves the same records over HTTP and pushes a new snapshot whenever the
store changes. Structure:

    writ/api.py       the read model: store in, JSON out, no HTTP
    writ/server.py    routing, the event stream, static files
    ui/src/*.ts       the app, compiled to writ/static/ by ui/build.mjs

Three properties worth stating plainly, because they are what make this safe to
leave running:

**Read-only.** There is no route that writes. Not "no route the UI calls" — no
route that exists. A page cannot dispatch, cancel, override, or rule on a
decision, which is why it needs no auth token, no CSRF defence, and no
confirmations, and why leaving a tab open cannot cost anything.

**Loopback by default.** The page has no authentication and does not need any
while only this machine can reach it. Binding elsewhere warns.

**No runtime dependencies.** `http.server`, one compiled script, one stylesheet.
The TypeScript is a contributor-time tool; `pip install writ` needs no node.
"""

from __future__ import annotations

import errno
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import api, state
from .state import WritError

DEFAULT_PORT = 8731

#: How often the watcher stats `state.json`. Agent turns take tens of seconds, so
#: this is far finer than what it observes; it is cheap because a tick is a stat
#: and the file is only parsed when the mtime moves.
POLL_SECONDS = 0.4

#: Sent when nothing has changed, so an idle stream is not mistaken for a dead one
#: by a browser or an intermediary.
HEARTBEAT_SECONDS = 15.0

STATIC = Path(__file__).parent / "static"

#: Only these, and only from the package directory. There is no path joining of
#: anything a client sends, so there is no traversal to defend against.
ASSETS = {
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>writ</title>
<link rel="stylesheet" href="style.css">
</head>
<body></body>
<script src="app.js"></script>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    """Routing only. Every payload comes from `writ.api`."""

    root: Path
    server_version = "writ"
    sys_version = ""
    protocol_version = "HTTP/1.1"  # so keep-alive works for the event stream

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        path = self.path.split("?", 1)[0]
        clean = path.rstrip("/") or "/"
        # Whether a status line has gone out, so a late failure knows if a 500 is
        # still possible. http.server does not track this.
        self.started = False
        try:
            if clean == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path in ASSETS:
                name, kind = ASSETS[path]
                self._send(200, kind, (STATIC / name).read_bytes())
            elif clean == "/events":
                self._stream()
            elif clean.startswith("/api/"):
                self._api(clean[len("/api/") :])
            else:
                self._text(404, "not found\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tab closed mid-response; nothing to send and nobody to tell
        except Exception:
            # An unexpected fault used to escape here, which closed the socket
            # with no status line at all: the page showed a bare network error,
            # indistinguishable from writ having been stopped. Answer with a 500
            # so the reader knows the server is up and something inside it broke,
            # then re-raise so the traceback still reaches the terminal.
            self._fail()
            raise

    def _fail(self) -> None:
        """Send a 500, if a response has not already started."""
        if self.started:
            return  # mid-body: the status line is long gone, so say nothing more
        try:
            self._json(500, {"error": "writ serve hit an internal error; see its terminal"})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the client is gone too; the terminal traceback is the record

    def _api(self, rest: str) -> None:
        parts = [segment for segment in rest.split("/") if segment]
        try:
            payload = self._route(parts)
        except KeyError as exc:
            self._text(404, f"no such id: {exc.args[0]}\n")
            return
        except ValueError as exc:
            self._text(400, f"bad request: {exc}\n")
            return
        except WritError as exc:
            # A corrupt or missing store is the operator's problem, not a crash.
            self._json(500, {"error": str(exc)})
            return
        if payload is None:
            self._text(404, "not found\n")
            return
        if isinstance(payload, str):
            self._send(200, "text/plain; charset=utf-8", payload.encode())
            return
        self._json(200, payload)

    def _route(self, parts: list[str]) -> Any:
        if parts == ["snapshot"]:
            return api.everything(self.root)
        data = state.load(self.root)
        if parts == ["overview"]:
            return api.overview(data)
        if parts == ["tasks"]:
            return api.tasks(data)
        if parts == ["runs"]:
            return api.runs(data)
        if parts == ["decisions"]:
            return api.decisions(data)
        if parts == ["milestones"]:
            return api.milestones(data)
        if parts == ["graph"]:
            return api.graph(data)
        if parts == ["activity"]:
            return api.activity(data)
        if len(parts) == 2 and parts[0] == "task":
            return api.task(data, parts[1])
        if len(parts) == 2 and parts[0] == "milestone":
            return api.milestone(data, parts[1])
        if len(parts) == 2 and parts[0] == "run":
            return api.run(data, self.root, parts[1])
        if len(parts) == 3 and parts[0] == "run":
            # The whole log rather than the tail the detail view carries.
            return api.log(self.root, data, parts[1], parts[2])
        return None

    # ------------------------------------------------------------- responses

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.started = True
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page loads nothing remote and posts nowhere; say so, so a stray
        # injected string cannot turn into a request.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, "application/json", json.dumps(payload).encode())

    def _text(self, code: int, body: str) -> None:
        self._send(code, "text/plain; charset=utf-8", body.encode())

    # ------------------------------------------------------------- streaming

    def _stream(self) -> None:
        """Push a snapshot whenever `state.json` changes.

        Watching the file rather than hooking the orchestrator is what makes this
        work for every writer: `writ run`, a hand-run `writ dispatch`, a `writ
        override` in another window, even an editor saving the file. The dashboard
        needs to know nothing about who is working.
        """
        self.started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        path = state.state_file(self.root)
        last: tuple[float, int] | None = None
        idle = 0.0
        while not getattr(self.server, "closing", False):
            try:
                stat = path.stat()
                stamp = (stat.st_mtime, stat.st_size)
            except OSError:
                stamp = None
            if stamp is not None and stamp != last:
                last = stamp
                # A writer may be mid-rewrite. A torn read is not worth dropping
                # the stream over: skip it and catch the next tick.
                try:
                    payload = json.dumps(api.everything(self.root))
                except Exception:
                    pass
                else:
                    self._event("snapshot", payload)
                    idle = 0.0
            elif idle >= HEARTBEAT_SECONDS:
                self._event("ping", "{}")
                idle = 0.0
            threading.Event().wait(POLL_SECONDS)
            idle += POLL_SECONDS

    def _event(self, name: str, payload: str) -> None:
        """Write one event, letting a vanished client end the loop.

        A closed tab is discovered by writing to it, not by asking. Both errors
        below mean the far end is gone; `_stream`'s caller swallows them, which is
        how the thread and its held connection are released.
        """
        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    def log_message(self, *args: Any) -> None:
        """Silence per-request logging: a polling stream would flood the shell."""

    def handle_one_request(self) -> None:
        """Treat a client vanishing between requests as ordinary.

        `protocol_version` is HTTP/1.1, so a connection is kept alive and the
        thread parks in `readline()` waiting for the next request on it. Browsers
        reap idle connections routinely, and some do it with an RST rather than a
        clean shutdown — Safari and Chrome both do after a few seconds idle, and
        every navigation and reload leaves connections behind to be collected.

        `socketserver` sends that to `handle_error`, which prints a traceback. So
        merely leaving the page open produced a twenty-line crash report every
        few seconds, describing something that had gone entirely correctly. The
        real cost is not noise: a log that cries wolf on a healthy idle socket is
        one an operator learns to skip, including on the day it reports a genuine
        fault.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            # Nothing to answer and nobody to answer to. Closing the connection
            # is the whole remedy.
            self.close_connection = True


def _server_class() -> type[ThreadingHTTPServer]:
    """A threading server that does not report a hung-up client as a crash.

    `handle_one_request` covers the common case, but a client can also vanish
    mid-body, and every such site would otherwise need its own guard. This is the
    one place they all funnel through, so the classification lives here: a
    disconnect is routine and silent, and anything else still gets the full
    traceback it deserves.
    """

    class Server(ThreadingHTTPServer):
        closing = False

        def handle_error(self, request: Any, client_address: Any) -> None:
            if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
                return  # the far end went away; there is nothing to report
            super().handle_error(request, client_address)

    return Server


def serve(
    root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    """Serve the dashboard until interrupted."""
    if not STATIC.joinpath("app.js").exists():  # pragma: no cover - packaging
        raise WritError(
            f"the dashboard assets are missing from {STATIC} "
            "(build them with: node ui/build.mjs)"
        )
    state.load(root)  # fail here, with writ's own message, not on first request

    handler = type("Handler", (_Handler,), {"root": root})
    try:
        server = _server_class()((host, port), handler)
    except OSError as exc:
        # A traceback says nothing a reader can act on. Suggest another port only
        # when the port is the problem: on a bad --host that would be confident
        # and useless advice.
        hint = (
            f"  (try --port {port + 1})"
            if exc.errno in (errno.EADDRINUSE, errno.EACCES)
            else ""
        )
        raise WritError(
            f"cannot serve on {host}:{port}: {exc.strerror or exc}{hint}"
        ) from exc

    server.daemon_threads = True  # a held-open stream must not block shutdown
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    url = f"http://{shown}:{server.server_port}/"
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"warning: serving on {host} — anyone who can reach this port can read "
            "your design, tasks, agent prompts and logs; there is no authentication",
            file=sys.stderr,
            flush=True,
        )
    print(f"writ serve on {url}", flush=True)
    print("read-only; following .writ/state.json  (^C to stop)", flush=True)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.closing = True
        server.shutdown()
        server.server_close()
