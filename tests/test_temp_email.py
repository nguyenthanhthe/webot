"""Unit tests for wvbot.temp_email (Cloudflare Temp Email & Mail.tm clients)."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from wvbot.temp_email import (
    Mailbox,
    MailTmClient,
    TempEmailClient,
    TempEmailError,
    extract_verification_code,
    main as temp_email_main,
)


class FakeTempEmailHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        if self.path == "/admin/new_address":
            auth = self.headers.get("x-admin-auth")
            if auth != "test_secret":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"unauthorized"}')
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode())
            name = payload.get("name") or "bot"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"address": f"{name}@example.com", "jwt": "fake.jwt.token"}).encode()
            self.wfile.write(resp)
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/api/parsed_mails"):
            auth = self.headers.get("Authorization")
            if not auth or "fake.jwt.token" not in auth:
                self.send_response(401)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            messages = [
                {
                    "id": "1",
                    "subject": "Wolvesville confirmation",
                    "text": "Welcome to Wolvesville! Your code is 829103 to verify.",
                }
            ]
            self.wfile.write(json.dumps(messages).encode())
            return

        if self.path.startswith("/api/parsed_mail/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"id": "2", "subject": "Verify", "text": "Your code is 445566"}
                ).encode()
            )
            return

        self.send_response(404)
        self.end_headers()


class TempEmailTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), FakeTempEmailHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_url_scheme_validation(self) -> None:
        for bad in ("", "file:///etc/passwd", "ftp://example.com", "javascript:alert(1)", "not-a-url"):
            with self.assertRaises(TempEmailError):
                TempEmailClient(bad)

    def test_extract_verification_code_and_html_css_filtering(self) -> None:
        self.assertEqual(
            TempEmailClient.extract_verification_code("Your verification code is 123456."),
            "123456",
        )
        self.assertEqual(
            extract_verification_code("Click https://wolvesville.com/verify?token=abc"),
            "https://wolvesville.com/verify?token=abc",
        )
        # CSS hex color / style attributes must not produce false 6-digit OTPs
        self.assertIsNone(
            extract_verification_code(
                "<style>.btn{color:#123456;width:100000px}</style><div data-id='654321'>Hello</div>"
            )
        )
        self.assertEqual(
            extract_verification_code(
                "<style>body{color:#123456}</style><p>Your verification code is <b>987654</b></p>"
            ),
            "987654",
        )
        self.assertEqual(
            extract_verification_code("Color #123456 and your pin: 654321"),
            "654321",
        )
        self.assertIsNone(TempEmailClient.extract_verification_code("No codes here"))
        self.assertIsNone(TempEmailClient.extract_verification_code(""))

    def test_create_mailbox(self) -> None:
        client = TempEmailClient(f"http://127.0.0.1:{self.port}", admin_password="test_secret")
        box = client.create_mailbox(name="alpha")
        self.assertEqual(box.address, "alpha@example.com")
        self.assertEqual(box.jwt, "fake.jwt.token")

    def test_create_mailbox_unauthorized(self) -> None:
        client = TempEmailClient(f"http://127.0.0.1:{self.port}", admin_password="wrong_password")
        with self.assertRaises(TempEmailError):
            client.create_mailbox(name="alpha")

    def test_list_messages_and_wait_code(self) -> None:
        client = TempEmailClient(f"http://127.0.0.1:{self.port}")
        messages = client.list_messages("fake.jwt.token")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "1")

        code = client.wait_for_verification_code("fake.jwt.token", timeout_sec=2.0, poll_interval_sec=0.1)
        self.assertEqual(code, "829103")

    def test_wait_code_fetches_detail_when_summary_empty_and_aborts_on_401(self) -> None:
        client = TempEmailClient(f"http://127.0.0.1:{self.port}")
        with self.assertRaises(TempEmailError):
            client.wait_for_verification_code("bad.jwt", timeout_sec=2.0, poll_interval_sec=0.05)

        with patch.object(client, "list_messages", return_value=[{"id": "2", "subject": "Verify"}]):
            code = client.wait_for_verification_code("fake.jwt.token", timeout_sec=1.0, poll_interval_sec=0.05)
            self.assertEqual(code, "445566")


class MailTmClientTest(unittest.TestCase):
    def test_create_mailbox_requires_active_domains_no_hardcoded_fallback(self) -> None:
        client = MailTmClient()
        with patch.object(client, "get_domains", return_value=[]):
            with self.assertRaises(TempEmailError):
                client.create_mailbox(name="bot")

    def test_create_mailbox_rejects_unavailable_requested_domain(self) -> None:
        client = MailTmClient()
        with patch.object(client, "get_domains", return_value=["active.example"]):
            with self.assertRaises(TempEmailError):
                client.create_mailbox(name="bot", domain="missing.example")

    def test_create_mailbox_retries_only_on_conflict(self) -> None:
        client = MailTmClient()
        with patch.object(client, "get_domains", return_value=["active.example"]):
            # 500 server error must not retry blindly
            with patch.object(
                client, "_req", side_effect=TempEmailError("server error", status_code=500)
            ) as mock_req:
                with self.assertRaises(TempEmailError):
                    client.create_mailbox(name="bot")
                self.assertEqual(mock_req.call_count, 1)

            # 422/409 conflict retries once with random suffix then requests /token
            with patch.object(
                client,
                "_req",
                side_effect=[
                    TempEmailError("conflict", status_code=422),
                    {"id": "acc2"},
                    {"token": "jwt_ok"},
                ],
            ) as mock_req:
                box = client.create_mailbox(name="bot")
                self.assertIsInstance(box, Mailbox)
                self.assertTrue(box.address.startswith("bot_"))
                self.assertEqual(box.jwt, "jwt_ok")
                self.assertEqual(mock_req.call_count, 3)

    def test_wait_for_verification_code_retries_transient_get_message_and_reads_html_list(self) -> None:
        client = MailTmClient()
        calls = {"get": 0}

        def fake_get_message(_jwt: str, _msg_id: str) -> dict:
            calls["get"] += 1
            if calls["get"] == 1:
                raise TempEmailError("temporary glitch", status_code=503)
            return {
                "subject": "Welcome",
                "text": "",
                "intro": "Hello user, welcome to Wolvesville!",
                "html": ["<style>a{color:#112233}</style>", "<p>Code: <b>739201</b></p>"],
            }

        with (
            patch.object(client, "list_messages", return_value=[{"id": "m1"}]),
            patch.object(client, "get_message", side_effect=fake_get_message),
        ):
            code = client.wait_for_verification_code("jwt", timeout_sec=2.0, poll_interval_sec=0.02)
            self.assertEqual(code, "739201")
            self.assertEqual(calls["get"], 2)

    def test_cli_main_supports_auto_mailtm(self) -> None:
        with patch("wvbot.temp_email.MailTmClient.create_mailbox", return_value=Mailbox("a@b.c", "j1")):
            rc = temp_email_main(["create", "--url", "auto", "--name", "a"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
