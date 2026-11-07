"""Unit tests for tools/temp_email.py (Cloudflare Temp Email client)."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import unittest

from tools.temp_email import TempEmailClient, TempEmailError


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

    def test_extract_verification_code(self) -> None:
        self.assertEqual(
            TempEmailClient.extract_verification_code("Your verification code is 123456."),
            "123456",
        )
        self.assertEqual(
            TempEmailClient.extract_verification_code("Click https://wolvesville.com/verify?token=abc"),
            "https://wolvesville.com/verify?token=abc",
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


if __name__ == "__main__":
    unittest.main()
