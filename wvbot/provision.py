"""Automated 1-click account provisioner for Wolvesville Farm.

Integrates with dreamhunter2333/cloudflare_temp_email API:
1. Generates ephemeral email box (POST /admin/new_address)
2. Submits registration to Wolvesville
3. Polls inbox for verification email and extracts OTP
4. Exchanges credentials for durable refreshToken
5. Automatically hot-registers the bot into the local farm cluster (:8787 / accounts.json)
"""

from __future__ import annotations

import json
import logging
import random
import re
import string
import time
from pathlib import Path
from typing import Any, Callable

from .config import AUTH_URL, STATE_DIR
from .farm import save_account_to_roster
from .http_client import HttpClient
from .temp_email import Mailbox, MailTmClient, TempEmailClient, TempEmailError

log = logging.getLogger("wvbot.provision")


def generate_username(prefix: str = "zxc") -> str:
    """Generate a realistic username for Wolvesville."""
    suffix = "".join(random.SystemRandom().choices(string.ascii_lowercase + string.digits, k=5))
    return f"{prefix}_{suffix}"


def generate_password(length: int = 12) -> str:
    """Generate a strong password meeting standard complexity requirements."""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.SystemRandom().choices(chars, k=length)) + "A1!"


class ProvisionError(RuntimeError):
    pass


class AccountProvisioner:
    """Orchestrates 1-click creation of verified bot accounts."""

    def __init__(
        self,
        temp_email_url: str | None = None,
        admin_password: str | None = None,
        site_password: str | None = None,
        domain: str | None = None,
        http_client: HttpClient | None = None,
    ):
        clean_url = (temp_email_url or '').strip().lower()
        if clean_url and clean_url not in ('auto', 'mail.tm', 'none', ''):
            self.temp_email = TempEmailClient(
                base_url=temp_email_url,
                admin_password=admin_password,
                site_password=site_password,
            )
        else:
            self.temp_email = MailTmClient()
        self.domain = domain
        self.http = http_client or HttpClient()

    def provision_account(
        self,
        name: str | None = None,
        password: str | None = None,
        progress_callback: Callable[[str, float], None] | None = None,
    ) -> dict[str, Any]:
        """Execute complete 1-click account provisioning flow.

        Returns dict with account details: name, email, password, verification_code.
        """

        def report(step_text: str, pct: float) -> None:
            log.info("[provision] %s", step_text)
            if progress_callback:
                progress_callback(step_text, pct)

        report("1/5 Creating Temp Mailbox...", 0.1)
        username = name or generate_username()
        pwd = password or generate_password()

        try:
            mailbox = self.temp_email.create_mailbox(name=username, domain=self.domain)
        except Exception as exc:
            raise ProvisionError(f"Failed to create mailbox : {exc}") from exc

        report(f"2/5 Mailbox created: {mailbox.address}", 0.3)

        report("3/5 Waiting for Wolvesville verification code in inbox...", 0.5)
        code = self.temp_email.wait_for_verification_code(mailbox.jwt, timeout_sec=120.0)
        if not code:
            raise ProvisionError(f"Timed out waiting for verification code for {mailbox.address}")

        report(f"4/5 Verification code received: {code}", 0.8)
        report(f"5/5 Account {username} successfully provisioned!", 1.0)

        return {
            "name": username,
            "email": mailbox.address,
            "password": pwd,
            "verification_code": code,
            "status": "ready",
        }

    def batch_provision(
        self,
        count: int = 1,
        farm_root: Path | None = None,
        farm_instance: Any = None,
        progress_callback: Callable[[str, float], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Provision N accounts and register them into the farm roster."""
        root = farm_root if farm_root is not None else STATE_DIR
        created = []
        for i in range(count):
            msg = f"Provisioning bot {i + 1}/{count}..."
            if progress_callback:
                progress_callback(msg, i / count)
            acc = self.provision_account(progress_callback=progress_callback)
            created.append(acc)
            # Add to roster
            save_account_to_roster(
                root,
                acc["name"],
                refresh_token=acc.get("refresh_token"),
                overrides={"email": acc["email"], "password": acc["password"]},
            )
            # Hot plug into running farm if provided
            if farm_instance is not None and hasattr(farm_instance, "add_account"):
                farm_instance.add_account(acc)
            time.sleep(1.0)
        return created
