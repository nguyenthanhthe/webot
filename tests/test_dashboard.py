"""Unit tests for wvbot.dashboard."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from wvbot.dashboard import StatusStore, start_dashboard
from wvbot.farm import Farm, save_account_to_roster
from wvbot.stream import StreamHub


def _req(url: str, method: str = "GET", body: bytes | str | None = None, headers: dict[str, str] | None = None):
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            return exc.code, dict(exc.headers), raw
        finally:
            exc.close()


class StatusStoreTest(unittest.TestCase):
    def test_store_updates_and_event_ring_buffer(self):
        store = StatusStore(max_events=3)
        store.update(status="playing", level=10, xp_until_next=400)
        snap = store.get()
        self.assertEqual(snap["status"], "playing")
        self.assertEqual(snap["level"], 10)
        self.assertEqual(snap["xp_until_next"], 400)

        for i in range(5):
            store.add_event("info", f"evt-{i}")
        evts = store.events()
        self.assertEqual(len(evts), 3)
        self.assertEqual([e["message"] for e in evts], ["evt-2", "evt-3", "evt-4"])


class DashboardSecurityAndApiTest(unittest.TestCase):
    def test_token_auth_headers_query_and_unauthenticated_health(self):
        store = StatusStore()
        store.update(status="online", projection=123, xp_until_next=45, reflex_count=7, games_to_bio_break=2)
        with tempfile.TemporaryDirectory() as tmp:
            srv = start_dashboard(
                store=store,
                host="127.0.0.1",
                port=0,
                token="secret-tok-123",
                state_dir=Path(tmp),
            )
            port = srv.server_address[1]
            base = f"http://127.0.0.1:{port}"
            try:
                # /health is unauthenticated and returns minimal JSON without wildcard CORS
                code, hdrs, body = _req(f"{base}/health")
                self.assertEqual(code, 200)
                health = json.loads(body)
                self.assertIs(health["ok"], True)
                self.assertIn("version", health)
                self.assertNotIn("Access-Control-Allow-Origin", hdrs)

                # Protected endpoints reject missing/wrong token
                code, _, _ = _req(f"{base}/api/status")
                self.assertEqual(code, 401)
                code, _, _ = _req(f"{base}/")
                self.assertEqual(code, 401)
                code, _, _ = _req(f"{base}/api/status", headers={"Authorization": "Bearer wrong"})
                self.assertEqual(code, 401)

                # Accepted via Bearer, X-Dashboard-Token, or ?token=
                code, _, body = _req(f"{base}/api/status", headers={"Authorization": "Bearer secret-tok-123"})
                self.assertEqual(code, 200)
                data = json.loads(body)
                self.assertEqual(data["projection"], 123)
                self.assertEqual(data["xp_until_next"], 45)
                self.assertEqual(data["reflex_count"], 7)
                self.assertEqual(data["games_to_bio_break"], 2)

                code, _, _ = _req(f"{base}/api/status", headers={"X-Dashboard-Token": "secret-tok-123"})
                self.assertEqual(code, 200)
                code, _, html = _req(f"{base}/?token=secret-tok-123")
                self.assertEqual(code, 200)
                self.assertIn("sessionStorage", html)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_auto_token_generated_on_non_loopback_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            env = {k: v for k, v in os.environ.items() if k not in ("WV_BOT_DASHBOARD_TOKEN", "WV_DASHBOARD_TOKEN")}
            with patch.dict(os.environ, env, clear=True):
                srv = start_dashboard(
                    store=StatusStore(),
                    host="0.0.0.0",
                    port=0,
                    state_dir=state_dir,
                )
                try:
                    tok_file = state_dir / "dashboard_token.txt"
                    self.assertTrue(tok_file.exists())
                    disk_tok = tok_file.read_text(encoding="utf-8").strip()
                    self.assertGreaterEqual(len(disk_tok), 20)
                    self.assertEqual(srv.dashboard_token, disk_tok)
                    if os.name != "nt":
                        self.assertEqual(stat.S_IMODE(tok_file.stat().st_mode), 0o600)

                    port = srv.server_address[1]
                    code, _, _ = _req(f"http://127.0.0.1:{port}/api/status")
                    self.assertEqual(code, 401)
                    code, _, _ = _req(
                        f"http://127.0.0.1:{port}/api/status",
                        headers={"X-Dashboard-Token": disk_tok},
                    )
                    self.assertEqual(code, 200)
                finally:
                    srv.shutdown()
                    srv.server_close()

    def test_health_503_when_farm_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            farm = Farm([], bot_factory=lambda a: None, farm_root=Path(tmp))
            srv = farm.start_dashboard(host="127.0.0.1", port=0)
            port = srv.server_address[1]
            try:
                code, _, body = _req(f"http://127.0.0.1:{port}/health")
                self.assertEqual(code, 200)
                self.assertTrue(json.loads(body)["ok"])

                farm.stop_event.set()
                code, _, body = _req(f"http://127.0.0.1:{port}/health")
                self.assertEqual(code, 503)
                self.assertEqual(json.loads(body)["status"], "stopping")
            finally:
                srv.shutdown()
                srv.server_close()

    def test_post_validation_bounds_unknown_paths_and_delete_unquote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_account_to_roster(root, "botone", refresh_token="tok")

            class _FakeBot:
                def __init__(self, account):
                    self.stop_event = threading.Event()
                    self.pause_event = threading.Event()

                def run(self):
                    self.stop_event.wait(2.0)

            farm = Farm([{"name": "botone"}], bot_factory=lambda a: _FakeBot(a), max_bots=1, farm_root=root)
            srv = farm.start_dashboard(host="127.0.0.1", port=0)
            port = srv.server_address[1]
            base = f"http://127.0.0.1:{port}"
            try:
                # 415 when Content-Type is not JSON
                code, _, _ = _req(f"{base}/api/control", method="POST", body=b'{"action":"pause"}', headers={"Content-Type": "text/plain"})
                self.assertEqual(code, 415)

                # 413 when Content-Length exceeds MAX_POST_BYTES (exact boundary, no unread body RST race)
                import http.client
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
                try:
                    conn.putrequest("POST", "/api/control")
                    conn.putheader("Content-Type", "application/json")
                    conn.putheader("Content-Length", "65537")
                    conn.endheaders()
                    resp = conn.getresponse()
                    resp.read()
                    self.assertEqual(resp.status, 413)
                finally:
                    conn.close()

                # 404 on unknown POST path
                code, _, _ = _req(f"{base}/api/nonexistent", method="POST", body="{}", headers={"Content-Type": "application/json"})
                self.assertEqual(code, 404)

                # 400 on invalid /api/provision count (bool, 0, >20, string)
                for bad_count in (True, 0, 21, "abc"):
                    code, _, _ = _req(
                        f"{base}/api/provision",
                        method="POST",
                        body=json.dumps({"count": bad_count}),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(code, 400)

                # 409 when adding duplicate account by raw OR unnormalized alias does not erase/overwrite roster
                code, _, _ = _req(
                    f"{base}/api/accounts",
                    method="POST",
                    body=json.dumps({"name": "botone", "refresh_token": "tok2"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(code, 409)

                code, _, _ = _req(
                    f"{base}/api/accounts",
                    method="POST",
                    body=json.dumps({"name": "bot one", "refresh_token": "rt-overwrite"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(code, 409)
                roster_data = json.loads((root / "accounts.json").read_text(encoding="utf-8"))
                self.assertEqual(roster_data["accounts"], [{"name": "botone"}])
                self.assertEqual((root / "accounts" / "botone" / "refresh_token.txt").read_text(encoding="utf-8").strip(), "tok")

                # 409 when farm max_bots capacity is full does not write new account into accounts.json
                code, _, _ = _req(
                    f"{base}/api/accounts",
                    method="POST",
                    body=json.dumps({"name": "gamma", "refresh_token": "rt-gamma"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(code, 409)
                roster_data = json.loads((root / "accounts.json").read_text(encoding="utf-8"))
                self.assertEqual(roster_data["accounts"], [{"name": "botone"}])

                # /api/rooms with farm._room_probe when no active bot returns 502
                code, _, _ = _req(f"{base}/api/rooms")
                self.assertEqual(code, 502)

                # Standalone dashboard with rooms=None returns 503
                no_probe_srv = start_dashboard(store=StatusStore(), host="127.0.0.1", port=0, rooms=None)
                try:
                    code, _, _ = _req(f"http://127.0.0.1:{no_probe_srv.server_address[1]}/api/rooms")
                    self.assertEqual(code, 503)
                finally:
                    no_probe_srv.shutdown()
                    no_probe_srv.server_close()

                # DELETE /api/accounts/<url-encoded-name>
                code, _, _ = _req(f"{base}/api/accounts/bot%20one", method="DELETE")
                self.assertEqual(code, 200)
                self.assertEqual(farm.snapshot()["bots"], [])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_rooms_rewards_control_and_bot_detail_page(self):
        controls: list[tuple[str, str | None]] = []
        rewards: list[tuple[str, str | None]] = []
        store = StatusStore()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class _NamedBot:
                def __init__(self, account):
                    self.stop_event = threading.Event()
                    self.pause_event = threading.Event()
                    self.store = StatusStore()
                    self.store.update(status="playing", level=9)

                def run(self):
                    self.stop_event.wait(2.0)

            farm = Farm([{"name": "alpha"}], bot_factory=lambda a: _NamedBot(a), farm_root=root)

            def _probe(language="en", host=""):
                return [{"id": "r1", "gameMode": "quick", "language": language}]

            srv = start_dashboard(
                store=store,
                host="127.0.0.1",
                port=0,
                control=lambda a, bot_name=None: controls.append((a, bot_name)) or True,
                reward=lambda a, bot_name=None: rewards.append((a, bot_name)) or True,
                rooms=_probe,
                farm=farm,
                state_dir=root,
            )
            port = srv.server_address[1]
            base = f"http://127.0.0.1:{port}"
            try:
                code, _, body = _req(f"{base}/api/rooms")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["rooms"][0]["id"], "r1")

                # Reward & control actions via /api/control
                code, _, body = _req(
                    f"{base}/api/control",
                    method="POST",
                    body=json.dumps({"kind": "reward", "action": "lootbox", "bot": "alpha"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(code, 200)
                self.assertEqual(rewards, [("lootbox", "alpha")])

                code, _, _ = _req(
                    f"{base}/api/control",
                    method="POST",
                    body=json.dumps({"kind": "control", "action": "pause", "bot": "alpha"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(code, 200)
                self.assertEqual(controls, [("pause", "alpha")])

                # Unknown kind returns 400
                code, _, _ = _req(
                    f"{base}/api/control",
                    method="POST",
                    body=json.dumps({"kind": "invalid", "action": "launch_missiles"}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(code, 400)

                # Bot detail page exists and escapes name
                code, _, html = _req(f"{base}/bot/alpha")
                self.assertEqual(code, 200)
                self.assertIn("alpha", html)

                code, _, _ = _req(f"{base}/bot/missing_bot")
                self.assertEqual(code, 404)

                # XSS safety checks in main dashboard HTML
                code, _, main_html = _req(f"{base}/")
                self.assertEqual(code, 200)
                self.assertIn("function escHtml", main_html)
                self.assertIn("prov-site-auth", main_html)
                self.assertIn("Lootbox", main_html)
                self.assertIn("Wheel", main_html)
                self.assertIn("Projection", main_html)
                self.assertIn("Reflex:", main_html)
                self.assertIn("Bio-Break:", main_html)
                self.assertIn("To Next:", main_html)
                self.assertNotIn("onclick=", main_html.lower())
            finally:
                srv.shutdown()
                srv.server_close()

    def test_sse_stream_init_envelope_events_and_shutdown_exit(self):
        store = StatusStore()
        store.update(status="playing", level=3)
        hub = StreamHub()

        srv = start_dashboard(store=store, host="127.0.0.1", port=0, hub=hub)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        lines_seen: list[str] = []
        done = threading.Event()

        def _reader():
            try:
                with urllib.request.urlopen(f"{base}/api/stream", timeout=4.0) as resp:
                    for raw in resp:
                        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        lines_seen.append(text)
                        if "hello-sse" in text:
                            done.set()
            except Exception:
                done.set()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            time.sleep(0.15)
            hub.publish("event", {"msg": "hello-sse"})
            self.assertTrue(done.wait(2.5))
            joined = "\n".join(lines_seen)
            self.assertIn("event: init", joined)
            self.assertIn("event: event", joined)
            self.assertIn('"bots":', joined)
            self.assertIn("hello-sse", joined)
        finally:
            t0 = time.monotonic()
            srv.shutdown()
            srv.server_close()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())
            self.assertLess(time.monotonic() - t0, 2.0)


if __name__ == "__main__":
    unittest.main()
