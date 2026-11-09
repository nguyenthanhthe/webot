import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wvbot.farm import load_roster
from wvbot.provision import (
    AccountProvisioner,
    ProvisionError,
    generate_password,
    generate_username,
)
from wvbot.temp_email import Mailbox


class ProvisionTest(unittest.TestCase):
    def test_generate_credentials(self) -> None:
        user = generate_username("testbot")
        pwd = generate_password()
        self.assertTrue(user.startswith("testbot_"))
        self.assertGreaterEqual(len(pwd), 12)

    @patch("wvbot.provision.TempEmailClient")
    def test_provision_account_success(self, mock_client_cls) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_mailbox.return_value = Mailbox(
            address="bot123@mail.example.com", jwt="fake-jwt"
        )
        mock_client.wait_for_verification_code.return_value = "123456"

        provisioner = AccountProvisioner(
            temp_email_url="https://mail.example.com",
            admin_password="secret_admin",
        )
        acc = provisioner.provision_account(name="bot123")
        self.assertEqual(acc["name"], "bot123")
        self.assertEqual(acc["email"], "bot123@mail.example.com")
        self.assertEqual(acc["verification_code"], "123456")
        self.assertEqual(acc["status"], "ready")

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
        )
        with self.assertRaises(ProvisionError):
            provisioner.provision_account(name="bot123")

    @patch("wvbot.provision.TempEmailClient")
    def test_batch_provision_adds_to_roster(self, mock_client_cls) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_mailbox.side_effect = [
            Mailbox(address="bot1@mail.example.com", jwt="jwt1"),
            Mailbox(address="bot2@mail.example.com", jwt="jwt2"),
        ]
        mock_client.wait_for_verification_code.return_value = "654321"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provisioner = AccountProvisioner(
                temp_email_url="https://mail.example.com",
                admin_password="secret_admin",
            )
            created = provisioner.batch_provision(count=2, farm_root=root)
            self.assertEqual(len(created), 2)
            roster = load_roster(root)
            names = [acc["name"] for acc in roster]
            self.assertIn(created[0]["name"], names)
            self.assertIn(created[1]["name"], names)


if __name__ == "__main__":
    unittest.main()
