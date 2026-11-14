"""Unit tests for wvbot.provision."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wvbot import config as config_mod
from wvbot.farm import load_roster
from wvbot.provision import (
    AccountProvisioner,
    ProvisionError,
    generate_password,
    generate_username,
)
from wvbot.temp_email import Mailbox, TempEmailError


class ProvisionTest(unittest.TestCase):
    def test_generate_credentials(self) -> None:
        user = generate_username("testbot")
        pwd = generate_password()
        self.assertTrue(user.startswith("testbot_"))
        self.assertGreaterEqual(len(pwd), 12)

    @patch("wvbot.provision.TempEmailClient")
    def test_provision_requires_register_callback_fails_fast(self, mock_client_cls) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        provisioner = AccountProvisioner(
            temp_email_url="https://mail.example.com",
            admin_password="secret_admin",
        )
        with self.assertRaises(ProvisionError) as ctx:
            provisioner.provision_account(name="bot123")
        self.assertIn("register_fn", str(ctx.exception))
        mock_client.create_mailbox.assert_not_called()
        mock_client.wait_for_verification_code.assert_not_called()

    @patch("wvbot.provision.TempEmailClient")
    def test_provision_account_success_with_callbacks(self, mock_client_cls) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_mailbox.return_value = Mailbox(
            address="bot123@mail.example.com", jwt="fake-jwt"
        )
        mock_client.wait_for_verification_code.return_value = "123456"

        reg_calls: list[tuple[str, str, str]] = []
        ver_calls: list[tuple[str, str, str, str]] = []

        def fake_register(email: str, username: str, password: str) -> None:
            reg_calls.append((email, username, password))

        def fake_verify(email: str, username: str, password: str, code: str) -> dict:
            ver_calls.append((email, username, password, code))
            return {"refresh_token": "rt_verified_123"}

        provisioner = AccountProvisioner(
            temp_email_url="  https://mail.example.com  ",
            admin_password="secret_admin",
            register_fn=fake_register,
            verify_fn=fake_verify,
        )
        acc = provisioner.provision_account(name="bot123")
        self.assertEqual(acc["name"], "bot123")
        self.assertEqual(acc["email"], "bot123@mail.example.com")
        self.assertEqual(acc["verification_code"], "123456")
        self.assertEqual(acc["refresh_token"], "rt_verified_123")
        self.assertEqual(acc["status"], "ready")
        self.assertEqual(len(reg_calls), 1)
        self.assertEqual(ver_calls[0][3], "123456")
        mock_client_cls.assert_called_once_with(
            base_url="https://mail.example.com",
            admin_password="secret_admin",
            site_password=None,
        )

    @patch("wvbot.provision.TempEmailClient")
    def test_provision_account_timeout(self, mock_client_cls) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_mailbox.return_value = Mailbox(
            address="bot123@mail.example.com", jwt="fake-jwt"
        )
        mock_client.wait_for_verification_code.return_value = None

        provisioner = AccountProvisioner(
            temp_email_url="https://mail.example.com",
            admin_password="secret_admin",
            register_fn=lambda *_: None,
        )
        with self.assertRaises(ProvisionError):
            provisioner.provision_account(name="bot123")

    @patch("wvbot.provision.TempEmailClient")
    def test_provision_account_polling_error_wrapped(self, mock_client_cls) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_mailbox.return_value = Mailbox(
            address="bot123@mail.example.com", jwt="fake-jwt"
        )
        mock_client.wait_for_verification_code.side_effect = TempEmailError("unauthorized", status_code=401)

        provisioner = AccountProvisioner(
            temp_email_url="https://mail.example.com",
            admin_password="secret_admin",
            register_fn=lambda *_: None,
        )
        with self.assertRaises(ProvisionError):
            provisioner.provision_account(name="bot123")

    @patch("wvbot.provision.time.sleep")
    @patch("wvbot.provision.TempEmailClient")
    def test_batch_provision_adds_to_roster_and_never_writes_password(
        self, mock_client_cls, _mock_sleep
    ) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_mailbox.side_effect = [
            Mailbox(address="bot1@mail.example.com", jwt="jwt1"),
            Mailbox(address="bot2@mail.example.com", jwt="jwt2"),
        ]
        mock_client.wait_for_verification_code.return_value = "654321"

        fake_farm = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provisioner = AccountProvisioner(
                temp_email_url="https://mail.example.com",
                admin_password="secret_admin",
                register_fn=lambda *_: None,
                verify_fn=lambda email, user, _p, _c: {"refresh_token": f"tok_{user}"},
            )
            created = provisioner.batch_provision(
                count=2, farm_root=root, farm_instance=fake_farm
            )
            self.assertEqual(len(created), 2)
            roster = load_roster(root)
            names = [acc["name"] for acc in roster]
            self.assertIn(created[0]["name"], names)
            self.assertIn(created[1]["name"], names)
            raw_roster = json.loads((root / "accounts.json").read_text(encoding="utf-8"))
            for acc in raw_roster["accounts"]:
                self.assertNotIn("password", acc)
                self.assertNotIn("refresh_token", acc)
                tok_file = root / "accounts" / acc["name"] / "refresh_token.txt"
                self.assertEqual(tok_file.read_text(encoding="utf-8").strip(), f"tok_{acc['name']}")
            self.assertEqual(fake_farm.add_account.call_count, 2)

    @patch("wvbot.provision.TempEmailClient")
    def test_batch_provision_refuses_account_without_refresh_token(self, mock_client_cls) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_mailbox.return_value = Mailbox(
            address="bot1@mail.example.com", jwt="jwt1"
        )
        mock_client.wait_for_verification_code.return_value = "654321"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provisioner = AccountProvisioner(
                temp_email_url="https://mail.example.com",
                admin_password="secret_admin",
                register_fn=lambda *_: None,
            )
            with self.assertRaises(ProvisionError):
                provisioner.batch_provision(count=1, farm_root=root)
            self.assertFalse((root / "accounts.json").exists())

    @patch("wvbot.provision.MailTmClient")
    def test_provision_account_mailtm_default_and_url_aliases(self, mock_mailtm_cls) -> None:
        mock_client = MagicMock()
        mock_mailtm_cls.return_value = mock_client
        mock_client.create_mailbox.return_value = Mailbox(
            address="auto_bot@example.org", jwt="fake-mailtm-jwt"
        )
        mock_client.wait_for_verification_code.return_value = "777888"

        for url in (None, "", "  auto  ", "MAIL.TM"):
            provisioner = AccountProvisioner(
                temp_email_url=url,
                register_fn=lambda *_: {"refresh_token": "rt_auto"},
            )
            acc = provisioner.provision_account(name="auto_bot")
            self.assertEqual(acc["name"], "auto_bot")
            self.assertEqual(acc["email"], "auto_bot@example.org")
            self.assertEqual(acc["verification_code"], "777888")
            self.assertEqual(acc["refresh_token"], "rt_auto")
            self.assertEqual(acc["status"], "ready")

    @patch("wvbot.provision.MailTmClient")
    def test_batch_provision_resolves_state_dir_dynamically(self, mock_mailtm_cls) -> None:
        mock_client = MagicMock()
        mock_mailtm_cls.return_value = mock_client
        mock_client.create_mailbox.return_value = Mailbox(
            address="dyn@example.org", jwt="jwt"
        )
        mock_client.wait_for_verification_code.return_value = "112233"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(config_mod, "STATE_DIR", root):
                provisioner = AccountProvisioner(
                    register_fn=lambda *_: {"refresh_token": "rt_dyn"}
                )
                provisioner.batch_provision(count=1)
            self.assertTrue((root / "accounts.json").is_file())


if __name__ == "__main__":
    unittest.main()
