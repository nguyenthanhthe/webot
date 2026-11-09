"""CLI for 1-click Cloudflare Temp Email Bot Farm Provisioning.

Usage:
    python tools/provision_farm.py --url https://mail.example.com --admin myAdminPassword --count 2
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wvbot.config import STATE_DIR
from wvbot.provision import AccountProvisioner, ProvisionError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="1-Click Cloudflare Temp Email Farm Provisioner")
    parser.add_argument("--url", required=True, help="Base URL of Cloudflare Temp Email Worker")
    parser.add_argument("--admin", required=True, help="Admin password for Worker (/admin/new_address)")
    parser.add_argument("--site-auth", default=None, help="Optional x-custom-auth site password")
    parser.add_argument("--domain", default=None, help="Optional domain for email addresses")
    parser.add_argument("--count", type=int, default=1, help="Number of bot accounts to provision (default: 1)")

    args = parser.parse_args()

    print(f"=== 1-CLICK CLOUDFLARE TEMP MAIL FARM PROVISIONER ===")
    print(f"Worker URL: {args.url}")
    print(f"Target count: {args.count}")

    provisioner = AccountProvisioner(
        temp_email_url=args.url,
        admin_password=args.admin,
        site_password=args.site_auth,
        domain=args.domain,
    )

    try:
        created = provisioner.batch_provision(
            count=args.count,
            farm_root=STATE_DIR,
            progress_callback=lambda step, pct: print(f"[{int(pct*100)}%] {step}"),
        )
        print(f"\n[+] Successfully provisioned {len(created)} accounts into the Farm!")
        for acc in created:
            print(f"  - {acc['name']}: {acc['email']}")
        return 0
    except ProvisionError as exc:
        print(f"\n[-] Provisioning failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
