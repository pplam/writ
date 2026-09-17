"""A read-only web view of the graph that follows the run as it happens.

`writ graph` is a snapshot: you read it, it is already stale. While `writ run`
works through a DAG the interesting thing is *movement* — which tasks are live
right now, what just went to review, what a failure has parked. A terminal
redraw cannot show that without fighting the progress log for the same screen.

Three decisions shape this module:

**No dependencies.** writ installs with none, and a dashboard is not a good
reason to change that. So: `http.server` for the server, hand-rolled SVG for the
graph, server-sent events for the push. No framework, no build step, no bundler.

**Read-only.** There is no endpoint here that mutates anything. A browser tab
cannot dispatch, override, or cancel. That keeps the whole surface to two GETs
and means a page left open in a forgotten tab is inert, so this needs no CSRF
token, no auth, and no confirmation dialogs.

**The store is the state.** The server holds nothing between requests; it reads
`state.json` the same way every other command does. So a `writ run` in one
terminal, a `writ override` in another, and this page all agree, and restarting
the server loses nothing.
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

from . import render, state
from .model import acceptance_summary, effective_status
from .state import WritError

#: How often the watcher looks at `state.json`'s mtime. Agent turns last tens of
#: seconds, so this is far finer than the thing it observes; it is cheap because
#: a poll is a stat, and the file is only parsed when the mtime actually moves.
POLL_SECONDS = 0.4

#: Sent when nothing has changed, to keep proxies and browsers from deciding an
#: idle stream is a dead one.
HEARTBEAT_SECONDS = 15.0

#: Chosen to be memorable and out of the way of the usual dev-server ports.
DEFAULT_PORT = 8731

#: Node geometry, shared by the layout code and the SVG it emits.
NODE_WIDTH = 190
NODE_HEIGHT = 62
COLUMN_GAP = 96
ROW_GAP = 26
MARGIN = 32


def snapshot(root: Path) -> dict[str, Any]:
    """The whole view as one JSON-able object.

    One document rather than several endpoints: the graph, the statuses, and the
    counts have to agree with each other, and a page that fetched them
    separately could paint a node as running that the next request says is done.
    """
    data = state.load(root)
    tasks = data["tasks"]
    nodes = _layout(data, tasks)
    return {
        "project": data.get("project", {}).get("name", "writ"),
        "nodes": nodes,
        "edges": _edges(tasks, {node["id"]: node for node in nodes}),
        "milestones": _milestones(data),
        "totals": _totals(data, tasks),
        "decisions": _proposed(data),
        "width": max((n["x"] + NODE_WIDTH for n in nodes), default=0) + MARGIN,
        "height": max((n["y"] + NODE_HEIGHT for n in nodes), default=0) + MARGIN,
    }


def _layout(data: dict[str, Any], tasks: dict[str, Any]) -> list[dict[str, Any]]:
    """Place every task on a grid: dependency depth across, siblings down.

    Depth is the honest axis for this graph. It is what decides when a task can
    start, so a column is "work that could run at once" and the picture shows
    the parallelism the DAG allows. Ordering within a column follows the mean
    row of each task's dependencies, which keeps an edge from crossing the whole
    diagram when a simple chain would do.
    """
    levels = render.dag_levels(tasks)
    rows: dict[str, int] = {}
    nodes: list[dict[str, Any]] = []

    for column, level in enumerate(levels):
        def anchor(task_id: str) -> tuple[float, str]:
            deps = [d for d in tasks[task_id].get("depends_on", []) if d in rows]
            mean = sum(rows[d] for d in deps) / len(deps) if deps else -1.0
            return (mean, task_id)

        for row, task_id in enumerate(sorted(level, key=anchor)):
            rows[task_id] = row
            task = tasks[task_id]
            status = effective_status(data, task)
            counts = acceptance_summary(task)
            nodes.append(
                {
                    "id": task_id,
                    "title": task.get("title", ""),
                    "status": status,
                    "milestone": task.get("milestone", ""),
                    "passed": counts["passed"],
                    "total": counts["total"],
                    "depends_on": [
                        d for d in task.get("depends_on", []) if d in tasks
                    ],
                    "blocks": sorted(
                        other["id"]
                        for other in tasks.values()
                        if task_id in other.get("depends_on", [])
                    ),
                    "x": MARGIN + column * (NODE_WIDTH + COLUMN_GAP),
                    "y": MARGIN + row * (NODE_HEIGHT + ROW_GAP),
                    "column": column,
                }
            )
    return nodes


def _edges(
    tasks: dict[str, Any], placed: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """One entry per dependency, with the geometry to draw it.

    An edge is marked `satisfied` when its source is complete, which is what
    makes the picture readable at a glance: solid lines are settled history,
    faint ones are what the graph is still waiting on.
    """
    out = []
    for task_id, task in sorted(tasks.items()):
        for dep in task.get("depends_on", []):
            if dep not in placed or task_id not in placed:
                continue
            source, target = placed[dep], placed[task_id]
            out.append(
                {
                    "from": dep,
                    "to": task_id,
                    "x1": source["x"] + NODE_WIDTH,
                    "y1": source["y"] + NODE_HEIGHT / 2,
                    "x2": target["x"],
                    "y2": target["y"] + NODE_HEIGHT / 2,
                    "satisfied": source["status"] == "completed",
                }
            )
    return out


def _milestones(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for milestone in sorted(data["milestones"].values(), key=lambda m: m["id"]):
        tasks = [
            t for t in data["tasks"].values() if t.get("milestone") == milestone["id"]
        ]
        done = sum(1 for t in tasks if t["status"] == "completed")
        out.append(
            {
                "id": milestone["id"],
                "title": milestone.get("title", ""),
                "status": milestone.get("status", ""),
                "done": done,
                "total": len(tasks),
            }
        )
    return out


def _totals(data: dict[str, Any], tasks: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks.values():
        status = effective_status(data, task)
        counts[status] = counts.get(status, 0) + 1
    counts["tasks"] = len(tasks)
    counts["live"] = counts.get("running", 0) + counts.get("reviewing", 0)
    return counts


def _proposed(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Decisions waiting on a human, since no `writ run` will clear them."""
    return [
        {"id": item["id"], "title": item.get("title", ""), "by": item.get("by", "")}
        for item in data.get("decisions", [])
        if item.get("status") == "proposed"
    ]


class _Handler(BaseHTTPRequestHandler):
    """Two GETs: the page, and the event stream that keeps it current."""

    root: Path
    server_version = "writ"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif path == "/api/graph":
            body = json.dumps(snapshot(self.root)).encode()
            self._send(200, "application/json", body)
        elif path == "/events":
            self._stream()
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tab went away mid-write; nothing to recover

    def _stream(self) -> None:
        """Push a new snapshot whenever `state.json` changes.

        Watching the file rather than hooking the orchestrator is what makes this
        work for *any* writer: `writ run`, a hand-run `writ dispatch`, or a human
        typing `writ override` all land in the same file, and all of them show up
        here. The dashboard needs to know nothing about who is working.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        path = state.state_file(self.root)
        last: tuple[float, int] | None = None
        idle = 0.0
        try:
            while not getattr(self.server, "closing", False):
                try:
                    stat = path.stat()
                    stamp = (stat.st_mtime, stat.st_size)
                except OSError:
                    stamp = None
                if stamp is not None and stamp != last:
                    last = stamp
                    # A writer may be mid-rewrite; a torn read is not worth
                    # killing the stream over, so skip it and catch the next tick.
                    try:
                        payload = json.dumps(snapshot(self.root))
                    except Exception:
                        pass
                    else:
                        self._event("graph", payload)
                        idle = 0.0
                elif idle >= HEARTBEAT_SECONDS:
                    self._event("ping", "{}")
                    idle = 0.0
                threading.Event().wait(POLL_SECONDS)
                idle += POLL_SECONDS
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page was closed or reloaded

    def _event(self, name: str, payload: str) -> None:
        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    def log_message(self, *args: Any) -> None:
        """Silence the per-request log: an SSE poll would flood the terminal."""


def serve(
    root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    """Serve the dashboard until interrupted.

    Loopback by default. The page has no authentication — it does not need any
    while only this machine can reach it — so binding anywhere else publishes
    task titles, statuses and decision text to whoever can route to the port.
    That is a deliberate choice a caller can make, but not one to make silently.
    """
    handler = type("Handler", (_Handler,), {"root": root})
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        # A traceback here says nothing a reader can act on. Suggest another port
        # only when the port is what is wrong: on a bad --host, `--port N+1`
        # would be confident and useless advice.
        hint = (
            f"  (try --port {port + 1})"
            if exc.errno in (errno.EADDRINUSE, errno.EACCES)
            else ""
        )
        raise WritError(
            f"cannot serve on {host}:{port}: {exc.strerror or exc}{hint}"
        ) from exc
    server.closing = False
    server.daemon_threads = True  # a hung SSE stream must not block shutdown
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    url = f"http://{shown}:{server.server_port}/"
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"warning: serving on {host} — anyone who can reach this port can "
            "read your task titles and decisions; there is no authentication",
            file=sys.stderr,
        )
    print(f"dashboard on {url}", flush=True)
    print("watching .writ/state.json  (^C to stop)", flush=True)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.closing = True
        server.shutdown()
        server.server_close()


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>writ</title>
<style>
  :root {
    --ink: #1c1c1c; --dim: #6b6b6b; --line: #d8d8d8; --bg: #fbfbfa;
    --panel: #ffffff;
    --completed: #d8ece0; --review: #fdf0cf; --running: #d9e7f7;
    --failed: #f8d9d9; --planned: #f7f7f7; --cancelled: #eeeeee;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink: #e8e8e8; --dim: #9a9a9a; --line: #3a3a3a; --bg: #17181a;
      --panel: #1f2022;
      --completed: #24402f; --review: #45391a; --running: #1e3550;
      --failed: #4a2626; --planned: #262729; --cancelled: #242424;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
  }
  header {
    display: flex; align-items: baseline; gap: 18px; flex-wrap: wrap;
    padding: 14px 20px; border-bottom: 1px solid var(--line);
    background: var(--panel); position: sticky; top: 0; z-index: 5;
  }
  h1 { font-size: 15px; font-weight: 600; margin: 0; }
  h1 span { color: var(--dim); font-weight: 400; }
  .counts { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; }
  .counts b { font-weight: 600; }
  .counts .k { color: var(--dim); }
  #conn { margin-left: auto; font-size: 12px; color: var(--dim); }
  #conn.live b { color: #2f8f4e; }
  #conn.lost b { color: #c0392b; }
  main { padding: 20px; overflow: auto; }
  svg { display: block; }
  .edge { stroke: var(--line); fill: none; stroke-width: 1.5; }
  .edge.satisfied { stroke: var(--dim); }
  .edge.pending { stroke-dasharray: 4 4; }
  .node rect {
    stroke: var(--line); stroke-width: 1; rx: 7;
    transition: fill .25s ease;
  }
  .node.ready rect { stroke: var(--ink); stroke-width: 2; }
  .node.running rect, .node.reviewing rect { stroke: #4a7fb5; stroke-width: 2; }
  .node .id { font-size: 11px; fill: var(--dim); font-family: ui-monospace, monospace; }
  .node .title { font-size: 12.5px; fill: var(--ink); }
  .node .meta { font-size: 10.5px; fill: var(--dim); font-family: ui-monospace, monospace; }
  .node { cursor: default; }
  .node.live rect { animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .62; } }
  .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block;
            border: 1px solid var(--line); vertical-align: -1px; margin-right: 4px; }
  .bar { height: 3px; rx: 1.5; }
  aside {
    padding: 0 20px 24px; display: flex; gap: 28px; flex-wrap: wrap;
    font-size: 13px;
  }
  aside section { min-width: 220px; }
  aside h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
             color: var(--dim); font-weight: 600; margin: 0 0 6px; }
  aside li { list-style: none; margin: 0 0 3px; }
  aside ul { margin: 0; padding: 0; }
  code { font-family: ui-monospace, monospace; font-size: 12px; color: var(--dim); }
  .empty { color: var(--dim); padding: 40px 20px; }
</style>
</head>
<body>
<header>
  <h1>writ <span id="project"></span></h1>
  <div class="counts" id="counts"></div>
  <div id="conn"><b>connecting</b></div>
</header>
<main><div id="board" class="empty">loading…</div></main>
<aside>
  <section><h2>Milestones</h2><ul id="milestones"></ul></section>
  <section><h2>Decisions awaiting a ruling</h2><ul id="decisions"></ul></section>
  <section><h2>Status</h2><ul id="legend"></ul></section>
</aside>
<script>
const FILL = {
  completed: 'var(--completed)', 'awaiting-review': 'var(--review)',
  reviewing: 'var(--review)', running: 'var(--running)', starting: 'var(--running)',
  failed: 'var(--failed)', blocked: 'var(--failed)', cancelled: 'var(--cancelled)',
  ready: 'var(--panel)', planned: 'var(--planned)',
};
const ORDER = ['running', 'reviewing', 'awaiting-review', 'ready', 'planned',
               'completed', 'failed', 'blocked', 'cancelled'];
const NS = 'http://www.w3.org/2000/svg';
const W = __W__, H = __H__;

function el(name, attrs, text) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function clip(text, chars) {
  return text.length <= chars ? text : text.slice(0, chars - 1) + '…';
}

function draw(snap) {
  const board = document.getElementById('board');
  board.className = '';
  if (!snap.nodes.length) {
    board.className = 'empty';
    board.textContent = 'no tasks yet — run writ plan';
    return;
  }
  const svg = el('svg', {width: snap.width, height: snap.height,
                         viewBox: `0 0 ${snap.width} ${snap.height}`});

  for (const e of snap.edges) {
    // A cubic curve rather than a straight line: parallel diagonals between
    // columns are hard to follow, and the horizontal ends make it obvious which
    // side of a node an edge leaves and enters.
    const dx = Math.max(28, (e.x2 - e.x1) / 2);
    svg.appendChild(el('path', {
      class: 'edge ' + (e.satisfied ? 'satisfied' : 'pending'),
      d: `M ${e.x1} ${e.y1} C ${e.x1 + dx} ${e.y1}, ${e.x2 - dx} ${e.y2}, ${e.x2} ${e.y2}`,
    }));
  }

  for (const n of snap.nodes) {
    const live = n.status === 'running' || n.status === 'reviewing';
    const g = el('g', {
      class: `node ${n.status}` + (live ? ' live' : ''),
      transform: `translate(${n.x} ${n.y})`,
    });
    g.appendChild(el('title', {}, `${n.id}  ${n.title}\\n${n.status}` +
      `\\n${n.passed}/${n.total} acceptance criteria` +
      (n.depends_on.length ? `\\nafter: ${n.depends_on.join(', ')}` : '') +
      (n.blocks.length ? `\\nblocks: ${n.blocks.join(', ')}` : '')));
    g.appendChild(el('rect', {width: W, height: H, fill: FILL[n.status] || 'var(--planned)'}));
    g.appendChild(el('text', {class: 'id', x: 10, y: 17}, n.id));
    g.appendChild(el('text', {class: 'meta', x: W - 10, y: 17,
                              'text-anchor': 'end'}, `${n.passed}/${n.total}`));
    g.appendChild(el('text', {class: 'title', x: 10, y: 35}, clip(n.title, 26)));
    g.appendChild(el('text', {class: 'meta', x: 10, y: 51}, n.status));
    if (n.total) {
      g.appendChild(el('rect', {class: 'bar', x: 10, y: H - 9,
                                width: W - 20, height: 3, fill: 'var(--line)'}));
      g.appendChild(el('rect', {class: 'bar', x: 10, y: H - 9,
                                width: (W - 20) * n.passed / n.total, height: 3,
                                fill: 'var(--dim)'}));
    }
    svg.appendChild(g);
  }
  board.replaceChildren(svg);
}

function sidebar(snap) {
  document.getElementById('project').textContent = snap.project || '';

  const counts = ORDER.filter(s => snap.totals[s])
    .map(s => `<span><span class="swatch" style="background:${FILL[s]}"></span>` +
              `<b>${snap.totals[s]}</b> <span class="k">${s}</span></span>`);
  counts.unshift(`<span><b>${snap.totals.completed || 0}</b>` +
                 `<span class="k">/${snap.totals.tasks} done</span></span>`);
  document.getElementById('counts').innerHTML = counts.join('');

  document.getElementById('milestones').innerHTML = snap.milestones.map(m =>
    `<li><code>${m.id}</code> ${m.title} <span class="k">` +
    `${m.done}/${m.total}</span></li>`).join('') || '<li class="k">none</li>';

  document.getElementById('decisions').innerHTML = snap.decisions.map(d =>
    `<li><code>${d.id}</code> ${d.title}</li>`).join('') ||
    '<li><span class="k">nothing waiting</span></li>';

  document.getElementById('legend').innerHTML = ORDER.map(s =>
    `<li><span class="swatch" style="background:${FILL[s]}"></span>${s}</li>`).join('');
}

function connection(cls, text) {
  const box = document.getElementById('conn');
  box.className = cls;
  box.innerHTML = `<b>${text}</b>`;
}

function apply(snap) { draw(snap); sidebar(snap); }

// Fetch once so the page is useful before the first change arrives, then let the
// stream drive it. Without this a quiet project would render nothing at all.
fetch('api/graph').then(r => r.json()).then(apply);

let source;
function listen() {
  source = new EventSource('events');
  source.addEventListener('graph', e => { connection('live', 'live'); apply(JSON.parse(e.data)); });
  source.addEventListener('ping', () => connection('live', 'live'));
  source.onopen = () => connection('live', 'live');
  source.onerror = () => {
    connection('lost', 'reconnecting');
    source.close();
    setTimeout(listen, 1500);  // the server may just be restarting
  };
}
listen();
</script>
</body>
</html>
"""

# The node box size lives in Python because the layout maths needs it; the SVG
# needs the same numbers. Substituted rather than formatted: the page is full of
# CSS and JS braces, and every one of them would have to be escaped.
PAGE = PAGE.replace("__W__", str(NODE_WIDTH)).replace("__H__", str(NODE_HEIGHT))
