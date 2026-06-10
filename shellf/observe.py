"""Live observability dashboard for Shellf-Driving.

When enabled, this serves a local web page where a human can watch, in real time,
exactly what the LLM is doing through the MCP server:

  * a pixel-perfect mirror of each session's terminal, rendered by xterm.js from the
    raw PTY bytes we already capture (no re-rendering, no polling lag), and
  * a live timeline of every MCP tool call (name, args, timestamp, session).

It is OFF by default. Set SHELLF_OBSERVE_PORT=7331 (bound to 127.0.0.1) to turn it on.

Transport is Server-Sent Events over the stdlib HTTP server — no extra dependencies.
Late-joining browsers get a replay of recent output so the terminal isn't blank on
connect.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Per-session replay buffer cap (bytes) and global event history cap.
_BUFFER_CAP = 512 * 1024
_EVENT_HISTORY = 200


class EventHub:
    """Thread-safe pub/sub bridging the engine/server to connected browsers."""

    def __init__(self) -> None:
        self.active = False
        self._lock = threading.Lock()
        # name -> {meta..., "buffer": bytearray, "subs": set[Queue]}
        self._sessions: dict[str, dict] = {}
        self._event_subs: set[queue.Queue] = set()
        self._event_history: list[dict] = []

    # -- producers (called from the engine / MCP server) -------------------- #
    def register_session(self, name: str, cols: int, rows: int, command: str) -> None:
        with self._lock:
            s = self._sessions.get(name)
            if s is None:
                s = {"buffer": bytearray(), "subs": set()}
                self._sessions[name] = s
            s.update(cols=cols, rows=rows, command=command, alive=True)
        self.publish_event({
            "kind": "session", "session": name, "cols": cols, "rows": rows,
            "command": command, "alive": True,
        })

    def publish_output(self, name: str, data: bytes) -> None:
        if not self.active:
            return
        with self._lock:
            s = self._sessions.get(name)
            if s is None:
                return
            buf = s["buffer"]
            buf.extend(data)
            if len(buf) > _BUFFER_CAP:
                del buf[:-_BUFFER_CAP]
            subs = list(s["subs"])
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def mark_exited(self, name: str, status) -> None:
        with self._lock:
            if name in self._sessions:
                self._sessions[name]["alive"] = False
        self.publish_event({"kind": "session", "session": name,
                            "alive": False, "exit_status": status})

    def publish_event(self, event: dict) -> None:
        if not self.active:
            return
        event.setdefault("ts", time.time())
        with self._lock:
            self._event_history.append(event)
            if len(self._event_history) > _EVENT_HISTORY:
                self._event_history.pop(0)
            subs = list(self._event_subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # -- consumers (called from HTTP handlers) ------------------------------ #
    def subscribe_output(self, name: str) -> tuple[queue.Queue, bytes]:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            s = self._sessions.setdefault(name, {"buffer": bytearray(), "subs": set()})
            s["subs"].add(q)
            replay = bytes(s["buffer"])
        return q, replay

    def unsubscribe_output(self, name: str, q: queue.Queue) -> None:
        with self._lock:
            s = self._sessions.get(name)
            if s:
                s["subs"].discard(q)

    def subscribe_events(self) -> tuple[queue.Queue, list[dict]]:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._event_subs.add(q)
            # Seed with current sessions + recent events so a fresh page is populated.
            seed = [
                {"kind": "session", "session": n, "cols": s.get("cols", 80),
                 "rows": s.get("rows", 24), "command": s.get("command", ""),
                 "alive": s.get("alive", True)}
                for n, s in self._sessions.items()
            ] + list(self._event_history)
        return q, seed

    def unsubscribe_events(self, q: queue.Queue) -> None:
        with self._lock:
            self._event_subs.discard(q)


# Module-level singleton the server and engine share.
hub = EventHub()


# --------------------------------------------------------------------------- #
# HTTP / SSE server
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence default request logging
        pass

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = _INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self._stream_events()
        elif path.startswith("/stream/"):
            self._stream_output(path[len("/stream/"):])
        else:
            self.send_error(404)

    def _write_sse(self, payload: str) -> bool:
        try:
            self.wfile.write(b"data: " + payload.encode() + b"\n\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _stream_events(self):
        self._sse_headers()
        q, seed = hub.subscribe_events()
        try:
            for ev in seed:
                if not self._write_sse(json.dumps(ev)):
                    return
            while True:
                try:
                    ev = q.get(timeout=15)
                    if not self._write_sse(json.dumps(ev)):
                        return
                except queue.Empty:
                    if not self._write_sse(json.dumps({"kind": "ping"})):
                        return
        finally:
            hub.unsubscribe_events(q)

    def _stream_output(self, name: str):
        self._sse_headers()
        q, replay = hub.subscribe_output(name)
        try:
            if replay and not self._write_sse(base64.b64encode(replay).decode()):
                return
            while True:
                try:
                    data = q.get(timeout=15)
                    if not self._write_sse(base64.b64encode(data).decode()):
                        return
                except queue.Empty:
                    # SSE comment as heartbeat (ignored by EventSource).
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except OSError:
                        return
        finally:
            hub.unsubscribe_output(name, q)


_server: ThreadingHTTPServer | None = None


def start(port: int, host: str = "127.0.0.1") -> str:
    """Start the dashboard server once; returns the URL. Idempotent."""
    global _server
    if _server is not None:
        return f"http://{host}:{port}"
    _server = ThreadingHTTPServer((host, port), _Handler)
    _server.daemon_threads = True
    hub.active = True
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    return f"http://{host}:{port}"


_INDEX_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Shellf-Driving — live</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;font-family:ui-monospace,Menlo,monospace;background:#0b0b0f;color:#ddd;height:100vh;display:flex;flex-direction:column}
  header{padding:8px 14px;background:#16161f;border-bottom:1px solid #2a2a3a;display:flex;align-items:center;gap:10px}
  header b{color:#a78bfa}
  #wrap{flex:1;display:flex;min-height:0}
  #left{flex:1;display:flex;flex-direction:column;min-width:0}
  #tabs{display:flex;gap:6px;padding:6px 10px;background:#101018;overflow-x:auto}
  .tab{padding:4px 10px;border-radius:6px;background:#1d1d2a;cursor:pointer;white-space:nowrap;font-size:13px}
  .tab.active{background:#7c3aed;color:#fff}
  .tab.dead{opacity:.5}
  #terms{flex:1;position:relative;background:#000;min-height:0;overflow:hidden}
  .term{position:absolute;inset:0;display:none;align-items:center;justify-content:center;overflow:hidden}
  .term.active{display:flex}
  .scaler{transform-origin:center center}
  #log{width:380px;background:#0e0e16;border-left:1px solid #2a2a3a;display:flex;flex-direction:column}
  #log h3{margin:0;padding:8px 12px;font-size:12px;color:#888;border-bottom:1px solid #2a2a3a;letter-spacing:.08em}
  #events{flex:1;overflow:auto;padding:6px 8px;font-size:12px}
  .ev{padding:5px 7px;border-radius:5px;margin-bottom:4px;background:#15151f;border-left:3px solid #7c3aed}
  .ev.sess{border-left-color:#22c55e}
  .ev .nm{color:#a78bfa;font-weight:600}
  .ev .ar{color:#9aa;word-break:break-all}
  .ev .tm{color:#556;float:right}
  #status{margin-left:auto;font-size:12px;color:#677}
</style></head>
<body>
<header><b>Shellf-Driving</b> live observability <span id="status">connecting…</span></header>
<div id="wrap">
  <div id="left">
    <div id="tabs"></div>
    <div id="terms"></div>
  </div>
  <div id="log"><h3>MCP TOOL CALLS</h3><div id="events"></div></div>
</div>
<script>
const terms = {};       // name -> {term, el, streamES, meta}
let active = null;
const tabsEl = document.getElementById('tabs');
const termsEl = document.getElementById('terms');
const eventsEl = document.getElementById('events');
const statusEl = document.getElementById('status');

function fmtTime(ts){const d=new Date(ts*1000);return d.toLocaleTimeString();}

function selectTab(name){
  active=name;
  for(const n in terms){
    terms[n].el.classList.toggle('active', n===name);
    document.getElementById('tab-'+n)?.classList.toggle('active', n===name);
  }
  fit(name);
}

// Scale a session's fixed-grid terminal to fit the pane (byte-mirror stays at the
// PTY's exact cols/rows; we only change the visual size, never the grid).
function fit(name){
  const t=terms[name]; if(!t||!t.term.element) return;
  t.scaler.style.transform='scale(1)';
  const w=t.term.element.offsetWidth, h=t.term.element.offsetHeight;
  if(!w||!h) return;
  const pad=16, pw=termsEl.clientWidth-pad, ph=termsEl.clientHeight-pad;
  const s=Math.max(0.1, Math.min(pw/w, ph/h, 3));   // letterbox; cap upscale at 3x
  t.scaler.style.transform='scale('+s.toFixed(4)+')';
}

function ensureSession(meta){
  let t = terms[meta.session];
  if(!t){
    const el=document.createElement('div'); el.className='term';
    const scaler=document.createElement('div'); scaler.className='scaler';
    el.appendChild(scaler); termsEl.appendChild(el);
    const term=new Terminal({cols:meta.cols||80, rows:meta.rows||24, fontSize:14,
      theme:{background:'#000000'}, convertEol:false, disableStdin:true, cursorBlink:false});
    term.open(scaler);
    const tab=document.createElement('div'); tab.className='tab'; tab.id='tab-'+meta.session;
    tab.textContent=meta.session+' · '+(meta.command||'');
    tab.onclick=()=>selectTab(meta.session); tabsEl.appendChild(tab);
    // mirror raw PTY bytes straight into xterm
    const es=new EventSource('/stream/'+encodeURIComponent(meta.session));
    es.onmessage=(e)=>{const bin=atob(e.data);const u=new Uint8Array(bin.length);
      for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);term.write(u);};
    terms[meta.session]=t={term,el,scaler,es,meta};
    setTimeout(()=>fit(meta.session), 60);
    if(!active) selectTab(meta.session);
  }
  // Session (PTY) was resized -> match the grid exactly, then re-fit visually.
  if(meta.cols && meta.rows && (meta.cols!==t.meta.cols || meta.rows!==t.meta.rows)){
    t.term.resize(meta.cols, meta.rows); t.meta=meta; setTimeout(()=>fit(meta.session), 30);
  }
  const tab=document.getElementById('tab-'+meta.session);
  if(tab) tab.classList.toggle('dead', meta.alive===false);
}

// Browser window resize -> rescale the active terminal (grid is untouched).
new ResizeObserver(()=>{ if(active) fit(active); }).observe(termsEl);

function addEvent(ev){
  const div=document.createElement('div');
  div.className='ev'+(ev.kind==='session'?' sess':'');
  const args=ev.args?('<span class="ar">'+escapeHtml(JSON.stringify(ev.args))+'</span>'):'';
  const label = ev.kind==='session'
    ? ('session '+ev.session+(ev.alive===false?' exited('+(ev.exit_status??'')+')':' started'))
    : ev.name;
  div.innerHTML='<span class="tm">'+fmtTime(ev.ts)+'</span><span class="nm">'+escapeHtml(label||'')+'</span> '+args;
  eventsEl.insertBefore(div, eventsEl.firstChild);
  while(eventsEl.children.length>300) eventsEl.removeChild(eventsEl.lastChild);
}
function escapeHtml(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

const evES=new EventSource('/events');
evES.onopen=()=>statusEl.textContent='● connected';
evES.onerror=()=>statusEl.textContent='○ reconnecting…';
evES.onmessage=(e)=>{
  const ev=JSON.parse(e.data);
  if(ev.kind==='ping') return;
  if(ev.kind==='session') ensureSession(ev);
  if(ev.kind!=='session' || ev.alive===false) addEvent(ev);
  else addEvent(ev);
};
</script>
</body></html>
"""
