"""Unit tests for repository entrypoints, packaging, CLI tools, and syntax."""

from __future__ import annotations

import ast
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


class EntrypointTests(unittest.TestCase):
    def test_run_py_root_and_non_termux_forwarding(self) -> None:
        import run as run_mod

        self.assertEqual(run_mod.ROOT, ROOT)
        calls: list[list[str]] = []

        def fake_protocol_main() -> int:
            calls.append(list(sys.argv))
            return 0

        fake_mod = mock.MagicMock()
        fake_mod.main = fake_protocol_main

        with (
            mock.patch.object(run_mod, "PREFIX", Path(tempfile.gettempdir()) / "missing_termux_prefix"),
            mock.patch.object(run_mod, "_wake_lock") as wake_mock,
            mock.patch.dict(sys.modules, {"wvbot.__main__": fake_mod}),
            mock.patch.object(sys, "argv", ["run.py"]),
            redirect_stdout(io.StringIO()),
        ):
            rc = run_mod.main(["--mode", "protocol", "--extra-flag"])

        self.assertEqual(rc, 0)
        wake_mock.assert_not_called()
        self.assertEqual(calls, [["run.py", "--mode", "protocol", "--extra-flag"]])

    def test_setcreds_writes_token_with_state_dir_override(self) -> None:
        import setcreds

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "custom_state"
            with mock.patch.dict(os.environ, {"WV_BOT_STATE_DIR": str(state_dir)}):
                self.assertEqual(setcreds.resolve_state_dir(), state_dir)

            with redirect_stdout(io.StringIO()):
                rc = setcreds.main(
                    ["--state-dir", str(state_dir), "--account", "bot2", "--refresh-token", "tok_abc", "--no-restart"]
                )
            self.assertEqual(rc, 0)
            tok_path = state_dir / "accounts" / "bot2" / "refresh_token.txt"
            self.assertEqual(tok_path.read_text(encoding="utf-8").strip(), "tok_abc")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(tok_path.stat().st_mode), 0o600)


class ContainerAndConfigTests(unittest.TestCase):
    def test_config_json_synced_with_defaults(self) -> None:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("idle_restart_sec", cfg)
        self.assertEqual(cfg["dashboard_host"], "127.0.0.1")
        self.assertEqual(cfg["room_names"], ["vill win"])
        self.assertIn("dashboard_token", cfg)
        self.assertIn("vill_win_mode", cfg)
        self.assertIn("stay_in_room", cfg)

    def test_dockerfile_non_root_and_healthcheck(self) -> None:
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("STATE_DIR=/data", text)
        self.assertIn("USER wvbot", text)
        self.assertIn("http://127.0.0.1:8787/health", text)
        self.assertNotIn("COPY tools", text)

    def test_compose_binds_loopback_without_obsolete_version(self) -> None:
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("version:", text)
        self.assertIn("127.0.0.1:8787:8787", text)
        self.assertIn("./data:/data", text)


class PackageTests(unittest.TestCase):
    def test_package_check_and_build_zip_includes_all_wvbot_modules(self) -> None:
        from tools import package

        with redirect_stdout(io.StringIO()):
            self.assertEqual(package.main(["--check"]), 0)

        errors = package.verify_manifest()
        self.assertEqual(errors, [])

        with tempfile.TemporaryDirectory() as tmp:
            out_zip = Path(tmp) / "wvbot.zip"
            with redirect_stdout(io.StringIO()):
                rc = package.main(["--output", str(out_zip)])
            self.assertEqual(rc, 0)
            self.assertTrue(out_zip.is_file())
            with zipfile.ZipFile(out_zip) as zf:
                names = zf.namelist()
                self.assertEqual(len(names), len(set(names)))
                for mod in (
                    "wvbot/cdp.py",
                    "wvbot/ui.py",
                    "wvbot/provision.py",
                    "wvbot/temp_email.py",
                    "run.py",
                    "setcreds.py",
                    "config.json",
                    "payload.js",
                ):
                    self.assertIn(mod, names)


class TempEmailWrapperTests(unittest.TestCase):
    def test_reexports_from_wvbot_temp_email(self) -> None:
        from tools import temp_email as wrapper
        from wvbot import temp_email as impl

        self.assertIs(wrapper.TempEmailClient, impl.TempEmailClient)
        self.assertIs(wrapper.MailTmClient, impl.MailTmClient)
        self.assertIs(wrapper.Mailbox, impl.Mailbox)
        self.assertIs(wrapper.TempEmailError, impl.TempEmailError)
        self.assertTrue(callable(wrapper.extract_verification_code))
        self.assertTrue(hasattr(wrapper, "EmailMessage"))
        self.assertTrue(hasattr(wrapper, "site_password"))

    def test_cli_auto_selects_mailtm_and_custom_url_uses_cloudflare(self) -> None:
        from tools import temp_email as wrapper

        fake_mailtm = mock.MagicMock()
        fake_mailtm.create_mailbox.return_value = wrapper.Mailbox("bot@mail.tm", "jwt-mt")
        buf = io.StringIO()
        with (
            mock.patch.object(wrapper, "MailTmClient", return_value=fake_mailtm),
            redirect_stdout(buf),
        ):
            rc = wrapper.main(["create", "--url", "auto", "--name", "bot"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["address"], "bot@mail.tm")

        fake_cf = mock.MagicMock()
        fake_cf.wait_for_verification_code.return_value = "654321"
        buf2 = io.StringIO()
        with (
            mock.patch.object(wrapper, "TempEmailClient", return_value=fake_cf) as cf_cls,
            redirect_stdout(buf2),
        ):
            rc2 = wrapper.main(
                ["wait", "--url", "https://mail.example.com", "--site-auth", "sec", "--jwt", "j1"]
            )
        self.assertEqual(rc2, 0)
        cf_cls.assert_called_once_with(
            "https://mail.example.com",
            admin_password=None,
            site_password="sec",
        )
        self.assertEqual(json.loads(buf2.getvalue()), {"ok": True, "code": "654321"})


class RosterToolTests(unittest.TestCase):
    def test_offline_disk_operations_and_malformed_json_guard(self) -> None:
        from tools import roster

        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = roster.main(["list", "--offline", "--state-dir", str(sdir)])
            self.assertEqual(rc, 0)
            self.assertIn("single account", buf.getvalue())

            with redirect_stdout(io.StringIO()):
                rc_add = roster.main(
                    ["add", "bot1", "--refresh-token", "ref_1", "--offline", "--state-dir", str(sdir)]
                )
            self.assertEqual(rc_add, 0)
            self.assertTrue((sdir / "accounts.json").is_file())
            self.assertEqual(
                (sdir / "accounts" / "bot1" / "refresh_token.txt").read_text(encoding="utf-8").strip(),
                "ref_1",
            )

            with redirect_stdout(io.StringIO()):
                rc_rm = roster.main(["remove", "bot1", "--offline", "--state-dir", str(sdir)])
            self.assertEqual(rc_rm, 0)

            # Corrupt accounts.json must error rather than being silently overwritten.
            (sdir / "accounts.json").write_text("{corrupt", encoding="utf-8")
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                rc_bad = roster.main(["add", "bot2", "--offline", "--state-dir", str(sdir)])
            self.assertEqual(rc_bad, 1)
            self.assertEqual((sdir / "accounts.json").read_text(encoding="utf-8"), "{corrupt")

    def test_http_api_mode_and_auth_error_does_not_fallback(self) -> None:
        from tools import roster

        calls: list[tuple[str, str, str | None]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                calls.append(("GET", self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer good-tok":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"accounts": [{"name": "alpha", "ready": True}]}).encode())

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                calls.append(("POST", self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer good-tok":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "name": body.get("name")}).encode())

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}"
            with tempfile.TemporaryDirectory() as tmp:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = roster.main(["list", "--url", url, "--token", "good-tok", "--state-dir", tmp])
                self.assertEqual(rc, 0)
                self.assertIn("alpha", buf.getvalue())

                # Global flags before subcommand must not be overwritten by subparser defaults.
                buf_before = io.StringIO()
                with redirect_stdout(buf_before):
                    rc_add = roster.main(
                        [
                            "--url",
                            url,
                            "--token",
                            "good-tok",
                            "--state-dir",
                            tmp,
                            "add",
                            "gamma",
                            "--refresh-token",
                            "rt-gamma",
                        ]
                    )
                self.assertEqual(rc_add, 0)
                self.assertIn("gamma", buf_before.getvalue())
                self.assertIn(("POST", "/api/accounts", "Bearer good-tok"), calls)

                buf_off = io.StringIO()
                with redirect_stdout(buf_off):
                    rc_off = roster.main(["--offline", "--state-dir", tmp, "list"])
                self.assertEqual(rc_off, 0)
                self.assertIn("single account", buf_off.getvalue())

                # 401 from live server must fail instead of reading local disk.
                with mock.patch("sys.stderr", new_callable=io.StringIO):
                    rc_unauth = roster.main(["--url", url, "--token", "wrong", "--state-dir", tmp, "list"])
                self.assertEqual(rc_unauth, 1)
        finally:
            srv.shutdown()
            srv.server_close()


class WatchdogStatusAndDeployTests(unittest.TestCase):
    def test_status_parse_adb_devices_ignores_header_and_offline(self) -> None:
        from tools import status

        header_only = "List of devices attached\n\n"
        self.assertEqual(status.parse_adb_devices(header_only), [])
        self.assertFalse(status.has_connected_device(header_only))

        mixed = "List of devices attached\nemulator-5554\toffline\nR9PT609NVNT\tdevice\n"
        self.assertEqual(status.parse_adb_devices(mixed), ["R9PT609NVNT"])
        self.assertTrue(status.has_connected_device(mixed, "R9PT609NVNT"))
        self.assertFalse(status.has_connected_device(mixed, "emulator-5554"))

    def test_drill_watchdog_cli_flags_and_drill_flow(self) -> None:
        from tools import drill_watchdog

        with mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as ctx:
                drill_watchdog.build_parser().parse_args(["--help"])
            self.assertEqual(ctx.exception.code, 0)

        args = drill_watchdog.build_parser().parse_args(["--serial", "SER123", "--drill"])
        self.assertTrue(args.drill)
        self.assertEqual(args.serial, "SER123")

        with (
            mock.patch.object(drill_watchdog, "resolve_serial", return_value=""),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(drill_watchdog.main(["--check"]), 1)

        cmds: list[str] = []

        def fake_adb(cmd: str, serial: str) -> str:
            cmds.append(cmd)
            if "date +%s" in cmd:
                return "1700000000"
            if "cat " in cmd and "heartbeat.txt" in cmd:
                return "1699999990"
            if "cat " in cmd and "bot.lock/pid" in cmd:
                return "4242"
            return ""

        with (
            mock.patch.object(drill_watchdog, "adb_termux", side_effect=fake_adb),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(drill_watchdog.main(["--serial", "SER123", "--drill"]), 0)
        self.assertTrue(any("watchdog.sh" in c for c in cmds))

    def test_deploy_raises_on_nonzero_adb_exit(self) -> None:
        from tools import deploy

        failed = subprocess.CompletedProcess(["adb"], 1, stdout="", stderr="error: device not found")
        with (
            mock.patch.object(deploy, "resolve_serial", return_value="R9PT609NVNT"),
            mock.patch("subprocess.run", return_value=failed),
        ):
            with self.assertRaises(RuntimeError):
                deploy.adb("push", "a", "b")

    def test_provision_farm_parser_and_fail_fast(self) -> None:
        from tools import provision_farm
        from wvbot.provision import ProvisionError

        args = provision_farm.build_parser().parse_args(
            ["--url", "auto", "--count", "2", "--api-url", "http://127.0.0.1:8787", "--token", "t1"]
        )
        self.assertEqual(args.api_url, "http://127.0.0.1:8787")
        self.assertEqual(args.token, "t1")

        fake_prov = mock.MagicMock()
        fake_prov.batch_provision.side_effect = ProvisionError("no register_fn provided")
        with (
            mock.patch.object(provision_farm, "AccountProvisioner", return_value=fake_prov),
            redirect_stdout(io.StringIO()),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            rc = provision_farm.main(["--url", "auto"])
        self.assertEqual(rc, 1)


class SyntaxValidationTests(unittest.TestCase):
    def test_python_scripts_parse_cleanly(self) -> None:
        py_files = [ROOT / "run.py", ROOT / "setcreds.py", *sorted((ROOT / "tools").glob("*.py"))]
        for path in py_files:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_shell_scripts_pass_sh_syntax_check(self) -> None:
        sh_bin = shutil.which("sh")
        if sh_bin is None and Path(r"C:\Program Files\Git\usr\bin\sh.exe").is_file():
            sh_bin = r"C:\Program Files\Git\usr\bin\sh.exe"
        if sh_bin is None:
            self.skipTest("sh binary not available")

        scripts = [*sorted((ROOT / "tools").glob("*.sh")), ROOT / "tools" / "boot_wvbot"]
        self.assertTrue(scripts)
        for script in scripts:
            self.assertNotIn(b"\r", script.read_bytes(), msg=f"{script.name} contains CRLF")
            res = subprocess.run([sh_bin, "-n", str(script)], capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, msg=f"{script.name}: {res.stderr}")

    def test_node_scripts_pass_syntax_check(self) -> None:
        node_bin = shutil.which("node")
        if node_bin is None:
            self.skipTest("node binary not available")

        js_files = [
            ROOT / "web" / "bot.mjs",
            ROOT / "web" / "dashboard.mjs",
            ROOT / "web" / "login.mjs",
            ROOT / "web" / "payload.js",
            ROOT / "tools" / "scrape_rxzbot.mjs",
        ]
        for path in js_files:
            res = subprocess.run([node_bin, "--check", str(path)], capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, msg=f"{path.name}: {res.stderr}")


if __name__ == "__main__":
    unittest.main()
