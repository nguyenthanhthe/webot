/**
 * Tiny status server for the bot: one HTML page and two JSON endpoints.
 *
 * Everything the bot knows about itself ends up in `state`, so the monitor
 * on the laptop is just a browser tab pointing at http://127.0.0.1:8787/.
 */

import http from "node:http";

const MAX_EVENTS = 200;

export function createDashboard({ host = "0.0.0.0", port = 8787 } = {}) {
  const startedAt = Date.now();
  const state = {
    status: "starting",
    phase: null,
    room: null,
    game_id: null,
    role: null,
    players: 0,
    alive: 0,
    xp_total: 0,
    xp_last: 0,
    games_played: 0,
    wins: 0,
    kills: 0,
    reconnects: 0,
    errors: 0,
    last_error: null,
    started_at: startedAt,
    updated_at: startedAt,
    uptime_sec: 0,
  };
  const events = [];

  function update(fields) {
    Object.assign(state, fields);
    state.updated_at = Date.now();
    state.uptime_sec = Math.round((Date.now() - startedAt) / 1000);
  }

  function addEvent(kind, message) {
    events.push({ t: Date.now(), kind, message });
    if (events.length > MAX_EVENTS) events.shift();
  }

  function snapshot() {
    state.uptime_sec = Math.round((Date.now() - startedAt) / 1000);
    return { ...state };
  }

  const page = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Wolvesville bot</title>
<style>
 body{font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:#12151b;color:#e7ebf3;margin:0;padding:24px}
 h1{font-size:18px;margin:0 0 16px;font-weight:600}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
 .card{background:#1b1f27;border:1px solid #262c37;border-radius:10px;padding:12px 14px}
 .k{color:#8b93a7;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
 .v{font-size:22px;font-weight:600;margin-top:2px}
 .ok{color:#5ad07a}.warn{color:#f2c14e}.bad{color:#e56a6a}
 #events{margin-top:20px;background:#1b1f27;border:1px solid #262c37;border-radius:10px;padding:12px 14px;max-height:320px;overflow:auto}
 #events div{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#aeb6c8;padding:2px 0}
</style></head><body>
<h1>Wolvesville bot <span id="up" style="color:#8b93a7;font-weight:400"></span></h1>
<div class="grid" id="grid"></div>
<div id="events"></div>
<script>
const fields=[["status","status"],["phase","phase"],["room","room"],["role","role"],["players","players"],["alive","alive"],["xp_total","xp_total"],["xp_last","xp_last"],["games_played","games_played"],["wins","wins"],["kills","kills"],["reconnects","reconnects"],["errors","errors"],["last_error","last_error"],["uptime_sec","uptime_sec"]];
async function tick(){
  try{
    const s=await (await fetch('/api/status')).json();
    const g=document.getElementById('grid');
    g.innerHTML=fields.map(([k,l])=>{
      let v=s[k]; v=(v===null||v===undefined)?'-':v;
      let cls='';
      if(k==='status') cls= v==='playing'?'ok':(v==='error'?'bad':'warn');
      if(k==='errors'&&+v>0) cls='bad';
      return '<div class="card"><div class="k">'+l+'</div><div class="v '+cls+'">'+String(v).slice(0,40)+'</div></div>';
    }).join('');
    document.getElementById('up').textContent='· uptime '+s.uptime_sec+'s';
    const ev=await (await fetch('/api/events')).json();
    document.getElementById('events').innerHTML=ev.map(e=>'<div>'+new Date(e.t).toLocaleTimeString()+' ['+e.kind+'] '+e.message+'</div>').join('');
  }catch(e){document.getElementById('up').textContent='· offline ('+e.message+')';}
}
tick();setInterval(tick,2000);
</script></body></html>`;

  const server = http.createServer((req, res) => {
    if (req.url === "/api/status") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(snapshot(), null, 2));
    } else if (req.url === "/api/events") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(events.slice(-100)));
    } else if (req.url === "/" || req.url === "/index.html") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(page);
    } else {
      res.writeHead(404, { "content-type": "text/plain" });
      res.end("not found");
    }
  });

  return {
    state,
    update,
    addEvent,
    snapshot,
    listen: () =>
      new Promise((resolve) => {
        server.listen(port, host, () => {
          addEvent("dashboard", `dashboard listening on http://${host}:${port}`);
          resolve();
        });
      }),
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}
