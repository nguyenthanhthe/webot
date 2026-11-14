"""Automated account provisioner for Wolvesville Farm.

Integrates with ``cloudflare_temp_email`` or ``mail.tm`` to generate ephemeral
mailboxes, listen for verification emails, and register accounts that complete
interactive/browser verification into the farm roster.
"""

from __future__ import annotations

import logging
import random
import string
import time
from pathlib import Path
from typing import Any, Callable

from .farm import save_account_to_roster
from .http_client import HttpClient
from .temp_email import Mailbox, MailTmClient, TempEmailClient, TempEmailError

log = logging.getLogger("wvbot.provision")


def _resolve_state_dir() -> Path:
    from . import config

    getter = getattr(config, "get_state_dir", None)
    return Path(getter()) if callable(getter) else Path(config.STATE_DIR)


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


def _extract_refresh_token(result: Any) -> str | None:
    if isinstance(result, str):
        tok = result.strip()
        return tok or None
    if isinstance(result, dict):
        tok = result.get("refresh_token") or result.get("refreshToken")
        if isinstance(tok, str) and tok.strip():
            return tok.strip()
    return None


class AccountProvisioner:
    """Orchestrates creation of verified bot accounts via an external registration bridge."""

    def __init__(
        self,
        temp_email_url: str | None = None,
        admin_password: str | None = None,
        site_password: str | None = None,
        domain: str | None = None,
        http_client: HttpClient | None = None,
        register_fn: Callable[[str, str, str], Any] | None = None,
        verify_fn: Callable[[str, str, str, str], Any] | None = None,
    ):
        stripped_url = (temp_email_url or "").strip()
        clean_url = stripped_url.lower()
        if clean_url and clean_url not in ("auto", "mail.tm", "none"):
            self.temp_email: TempEmailClient | MailTmClient = TempEmailClient(
                base_url=stripped_url,
                admin_password=admin_password,
                site_password=site_password,
            )
        else:
            self.temp_email = MailTmClient()
        self.domain = domain
        self.http = http_client or HttpClient()
        self.register_fn = register_fn
        self.verify_fn = verify_fn

    def provision_account(
        self,
        name: str | None = None,
        password: str | None = None,
        progress_callback: Callable[[str, float], None] | None = None,
        register_fn: Callable[[str, str, str], Any] | None = None,
        verify_fn: Callable[[str, str, str, str], Any] | None = None,
        poll_timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        """Execute account provisioning flow with a browser/interactive registration callback.

        Fails fast when ``register_fn`` is not provided so callers never hang
        polling an untriggered mailbox or attempt to bypass Cloudflare Turnstile.
        """
        reg_cb = register_fn or self.register_fn
        ver_cb = verify_fn or self.verify_fn
        if reg_cb is None:
            raise ProvisionError(
                "Interactive/browser verification is required to register a Wolvesville "
                "account (Cloudflare Turnstile cannot be bypassed headlessly). "
                "Provide register_fn or import a refresh_token manually."
            )

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
            raise ProvisionError(f"Failed to create mailbox: {exc}") from exc

        report(f"2/5 Mailbox created: {mailbox.address}", 0.3)

        try:
            reg_result = reg_cb(mailbox.address, username, pwd)
        except ProvisionError:
            raise
        except Exception as exc:
            raise ProvisionError(f"Registration step failed for {mailbox.address}: {exc}") from exc

        refresh_token = _extract_refresh_token(reg_result)

        report("3/5 Waiting for Wolvesville verification code in inbox...", 0.5)
        try:
            code = self.temp_email.wait_for_verification_code(
                mailbox.jwt,
                timeout_sec=poll_timeout_sec,
            )
        except TempEmailError as exc:
            raise ProvisionError(f"Error polling verification email for {mailbox.address}: {exc}") from exc
        if not code:
            raise ProvisionError(f"Timed out waiting for verification code for {mailbox.address}")

        report(f"4/5 Verification code received: {code}", 0.8)

        if ver_cb is not None:
            try:
                ver_result = ver_cb(mailbox.address, username, pwd, code)
            except ProvisionError:
                raise
            except Exception as exc:
                raise ProvisionError(f"Verification step failed for {mailbox.address}: {exc}") from exc
            refresh_token = _extract_refresh_token(ver_result) or refresh_token

        report(f"5/5 Account {username} successfully provisioned!", 1.0)

        account: dict[str, Any] = {
            "name": username,
            "email": mailbox.address,
            "verification_code": code,
            "status": "ready" if refresh_token else "pending_token",
        }
        if refresh_token:
            account["refresh_token"] = refresh_token
        return account

    def batch_provision(
        self,
        count: int = 1,
        farm_root: Path | None = None,
        farm_instance: Any = None,
        progress_callback: Callable[[str, float], None] | None = None,
        register_fn: Callable[[str, str, str], Any] | None = None,
        verify_fn: Callable[[str, str, str, str], Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Provision N accounts and register validated refresh tokens into the farm roster."""
        if count < 1:
            raise ProvisionError("count must be at least 1")
        root = farm_root if farm_root is not None else _resolve_state_dir()
        created: list[dict[str, Any]] = []
        for i in range(count):
            msg = f"Provisioning bot {i + 1}/{count}..."
            if progress_callback:
                progress_callback(msg, i / count)
            acc = self.provision_account(
                progress_callback=progress_callback,
                register_fn=register_fn,
                verify_fn=verify_fn,
            )
            refresh_token = _extract_refresh_token(acc)
            if not refresh_token:
                raise ProvisionError(
                    f"Account {acc['name']} did not obtain a valid refresh_token; "
                    "refusing to save password-only credentials to roster."
                )
            save_account_to_roster(
                root,
                acc["name"],
                refresh_token=refresh_token,
                overrides={"email": acc["email"]},
            )
            if farm_instance is not None and hasattr(farm_instance, "add_account"):
                farm_instance.add_account(acc)
            created.append(acc)
            if i + 1 < count:
                time.sleep(1.0)
        return created
