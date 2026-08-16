#!/usr/bin/env python3
"""
AtlUserSuspend

Suspend all ACTIVE managed Atlassian Cloud accounts whose "Last active date"
is before a specified cutoff date.

Authentication:
  Expects a bearer token in the ATLASSIAN_TOKEN environment variable.
  Expects org ID via --org flag or ATLASSIAN_ORG environment variable.

Usage examples:
  # suspend all active accounts last active before 01.01.2023
  python AtlUserSuspend.py -d 01.01.2023

  # dry-run to preview what would be suspended
  python AtlUserSuspend.py -d 01.01.2023 --dry-run

  # specify org and output file
  python AtlUserSuspend.py --org YOUR_ORG_ID -d 01.06.2024 --out suspended.csv

  # exclude specific domains from suspension
  python AtlUserSuspend.py -d 01.01.2023 --exclude-domain example.com --exclude-domain corp.example.com
"""
import argparse
import base64
import csv
import os
import sys
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry

DEFAULT_SITE = "https://api.atlassian.com"
ADMIN_API_BASE_URL = f"{DEFAULT_SITE}/admin/v1"
USER_MGMT_API_BASE_URL = f"{DEFAULT_SITE}/users"


def get_site_base_url() -> str:
    """Return the Atlassian base URL from ATLASSIAN_SITE."""
    raw_site = (os.getenv("ATLASSIAN_SITE") or "").strip()
    if not raw_site:
        raise ValueError("Required environment variable ATLASSIAN_SITE is not set or is empty.")
    if raw_site.startswith("http://") or raw_site.startswith("https://"):
        return raw_site.rstrip("/")
    return f"https://{raw_site.rstrip('/')}"

YELLOW = '\033[33m'
GREEN = '\033[32m'
RED = '\033[31m'
RESET = '\033[0m'


def warn(msg: str):
    print(f"{YELLOW}Warning: {msg}{RESET}", file=sys.stderr)


def success(msg: str):
    print(f"{GREEN}{msg}{RESET}")


def error(msg: str):
    print(f"{RED}{msg}{RESET}", file=sys.stderr)


def create_request_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ValueError(f"Required environment variable {name} is not set or is empty.")
    return value


def get_org_auth_header() -> dict:
    """Auth for api.atlassian.com (org admin API) — always Bearer token."""
    token = (os.getenv("ATLASSIAN_TOKEN") or "").strip()
    if not token:
        print("Error: ATLASSIAN_TOKEN is required for org admin API calls.", file=sys.stderr)
        sys.exit(2)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def get_site_auth_header() -> dict:
    """Auth for site-level APIs (Jira/Confluence) — Basic with PAT when present."""
    jira_email = (os.getenv("JIRA_EMAIL") or "").strip()
    jira_pat = (os.getenv("JIRA_PAT") or "").strip()
    if jira_email and jira_pat:
        credentials = f"{jira_email}:{jira_pat}".encode("utf-8")
        encoded = base64.b64encode(credentials).decode("ascii")
        return {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}

    token = (os.getenv("ATLASSIAN_TOKEN") or "").strip()
    if token:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print("Error: No auth credentials available for site API.", file=sys.stderr)
    sys.exit(2)


def request_with_retries(session, method, url, headers=None, params=None, json=None, timeout=20):
    """Perform an HTTP request using a session configured with retries."""
    if session is None:
        session = create_request_session()
    return session.request(method, url, headers=headers, params=params, json=json, timeout=timeout)


def is_service_account_job_title(job_title: str) -> bool:
    if not job_title:
        return False
    return "service account" in (job_title or "").lower()


def get_user_profile(account_id, headers, session=None):
    """Retrieve a user's profile to inspect the job title before suspension."""
    if not account_id:
        return None
    url = f"{USER_MGMT_API_BASE_URL}/{account_id}/manage/profile"
    try:
        resp = request_with_retries(session, "GET", url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (RequestException, ValueError) as exc:
        warn(f"Could not read profile for {account_id}: {exc}")
        return None

    account = data.get("account", data) if isinstance(data, dict) else data
    extended_profile = account.get("extended_profile") if isinstance(account, dict) else {}
    jt = (
        account.get("job_title")
        if isinstance(account, dict) else None
    ) or (
        account.get("jobTitle")
        if isinstance(account, dict) else None
    ) or (
        extended_profile.get("job_title")
        if isinstance(extended_profile, dict) else None
    ) or (
        extended_profile.get("jobTitle")
        if isinstance(extended_profile, dict) else None
    )
    return jt if jt is not None else ""


def parse_last_active(last_active_str):
    """Parse the last_active ISO timestamp into a datetime object (UTC).

    Returns None if parsing fails or value is empty.
    """
    if not last_active_str:
        return None
    try:
        # handle various ISO formats from the API
        clean = last_active_str.replace('Z', '+00:00')
        return datetime.fromisoformat(clean)
    except (ValueError, TypeError):
        return None


def load_all_org_users(org_id, headers, session=None):
    """Paginate all managed users in the org.

    Returns list of dicts with email, account_id, name, account_status, last_active, product_access.
    """
    url = f"{ADMIN_API_BASE_URL}/orgs/{org_id}/users"
    all_users = []
    page = 0

    def _get(u):
        return request_with_retries(session, 'GET', u, headers=headers, timeout=30)

    while url:
        page += 1
        print(f"  Loading users page {page}...", end="\r")
        resp = _get(url)
        resp.raise_for_status()
        data = resp.json()

        for u in data.get("data", []):
            email = u.get("email")
            if email:
                product_access = u.get("product_access") or []
                # determine last_active: use top-level or max from product_access
                last_active_str = u.get("last_active")
                if not last_active_str and product_access:
                    # fall back to most recent product-level last_active
                    product_dates = [p.get("last_active") for p in product_access if p.get("last_active")]
                    if product_dates:
                        last_active_str = max(
                            product_dates,
                            key=lambda s: parse_last_active(s) or datetime.min.replace(tzinfo=timezone.utc)
                        )
                all_users.append({
                    "email": email,
                    "account_id": u.get("account_id") or u.get("accountId"),
                    "name": u.get("name", ""),
                    "account_status": u.get("account_status", ""),
                    "last_active": last_active_str,
                    "product_access": product_access,
                })

        next_link = data.get("links", {}).get("next")
        url = next_link if next_link else None

    print(f"  Loaded {len(all_users)} users across {page} pages.")
    return all_users

def suspend_user(account_id, headers, dry_run=False, session=None):
    """Suspend (disable) a user via the User Management lifecycle API."""
    if not account_id:
        warn("Cannot suspend user without account_id")
        return False

    url = f"{USER_MGMT_API_BASE_URL}/{account_id}/manage/lifecycle/disable"
    if dry_run:
        print(f"  DRY RUN: POST {url}")
        return True

    try:
        resp = request_with_retries(session, 'POST', url, headers=headers, timeout=20)
    except RequestException as e:
        error(f"  Suspend request failed for {account_id}: {e}")
        return False

    if resp.status_code in (200, 204):
        return True

    error(f"  Suspend failed for {account_id}: {resp.status_code} {resp.text}")
    return False


def domain_of(email: str):
    return email.split('@', 1)[1].lower() if email and '@' in email else None


def write_results_csv(path, results):
    """Write suspension outcomes to CSV, including skipped service-account users."""
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["email", "name", "account_id", "last_active", "action", "reason"])
        for u in results:
            w.writerow([
                u["email"],
                u["name"],
                u["account_id"],
                u["last_active"] or "never",
                u["action"],
                u["reason"],
            ])


def get_available_filename(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base}-{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def main():
    p = argparse.ArgumentParser(
        description="AtlUserSuspend: suspend all active managed accounts whose last active date "
                    "is before a specified cutoff date",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python AtlUserSuspend.py -d 01.01.2023\n"
            "  python AtlUserSuspend.py -d 01.01.2023 --dry-run\n"
            "  python AtlUserSuspend.py -d 01.06.2024 --out suspended.csv\n"
            "  python AtlUserSuspend.py -d 01.01.2023 --exclude-domain example.com\n"
            "  python AtlUserSuspend.py -d 01.01.2023 --include-never-active\n"
        )
    )
    p.add_argument('--org', required=False,
                   help='Organization ID (or set ATLASSIAN_ORG env var)')
    p.add_argument('-d', '--before-date', required=True,
                   help='Cutoff date in DD.MM.YYYY format. Accounts last active BEFORE this date will be suspended.')
    p.add_argument('--exclude-domain', action='append', default=[],
                   help='Domain(s) to exclude from suspension (can be specified multiple times)')
    p.add_argument('--include-never-active', action='store_true',
                   help='Also suspend accounts that have never been active (no last_active date)')
    p.add_argument('--out', required=False, default='atl_suspended_users.csv',
                   help='Output CSV file path (default: atl_suspended_users.csv)')
    p.add_argument('--dry-run', action='store_true',
                   help='Preview suspensions without performing them')
    args = p.parse_args()

    # Parse cutoff date
    try:
        cutoff_date = datetime.strptime(args.before_date, "%d.%m.%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"Error: Invalid date format '{args.before_date}'. Expected DD.MM.YYYY (e.g. 01.01.2023)", file=sys.stderr)
        sys.exit(2)

    # Org ID
    org_id = (args.org or os.getenv("ATLASSIAN_ORG") or "").strip()
    if not org_id:
        print("Error: Org ID required. Provide --org or set ATLASSIAN_ORG env var.", file=sys.stderr)
        sys.exit(1)
    if not args.org and os.getenv("ATLASSIAN_ORG"):
        warn("--org not provided, using ATLASSIAN_ORG from environment")

    try:
        get_required_env("ATLASSIAN_SITE")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    org_headers = get_org_auth_header()
    site_headers = get_site_auth_header()
    session = create_request_session()

    # Load ALL org users
    print(f"Loading all managed users from org {org_id}...")
    all_users = load_all_org_users(org_id, org_headers, session=session)

    if not all_users:
        print("No users found in org.")
        return

    # Filter: active accounts, last active before cutoff
    exclude_domains = set(d.lower().strip() for d in args.exclude_domain)
    candidates = []
    skipped_inactive = 0
    skipped_domain = 0
    skipped_recent = 0
    skipped_never_active = 0
    skipped_service_account = 0
    results = []

    for u in all_users:
        # only suspend active accounts (normalize casing and handle missing key)
        if (u.get("account_status") or "").lower() != "active":
            skipped_inactive += 1
            continue

        # exclude specific domains
        if exclude_domains and domain_of(u["email"]) in exclude_domains:
            skipped_domain += 1
            continue

        last_active_dt = parse_last_active(u["last_active"])

        if last_active_dt is None:
            # never active
            if args.include_never_active:
                candidates.append(u)
            else:
                skipped_never_active += 1
            continue

        if last_active_dt < cutoff_date:
            candidates.append(u)
        else:
            skipped_recent += 1

    # Report
    action = "DRY RUN" if args.dry_run else "LIVE"
    print(f"\nSuspend mode [{action}]: suspending active accounts last active before {args.before_date}")
    if exclude_domains:
        print(f"  Excluding domains: {', '.join(sorted(exclude_domains))}")
    print(f"  Candidates for suspension: {len(candidates)}")
    print(f"  Skipped (already inactive): {skipped_inactive}")
    print(f"  Skipped (excluded domain):  {skipped_domain}")
    print(f"  Skipped (active after cutoff): {skipped_recent}")
    print(f"  Skipped (never active, not included): {skipped_never_active}")
    print(f"  Skipped (Service Account): {skipped_service_account}")

    if not candidates:
        print("\nNo accounts to suspend.")
        return

    # Suspend
    suspended = []
    failed = 0

    for i, u in enumerate(candidates, 1):
        print(f"  [{i}/{len(candidates)}] Suspending {u['email']} (last active: {u['last_active'] or 'never'})...")
        job_title = get_user_profile(u["account_id"], org_headers, session=session)
        if job_title is None:
            warn(f"  Could not verify profile for {u['email']} — skipping")
            results.append({
                "email": u["email"],
                "name": u["name"],
                "account_id": u["account_id"],
                "last_active": u["last_active"],
                "action": "skipped",
                "reason": "Profile unavailable",
            })
            continue

        if is_service_account_job_title(job_title):
            skipped_service_account += 1
            print(f"{YELLOW}    Skipped: {u['email']} — Service Account — skipped{RESET}")
            results.append({
                "email": u["email"],
                "name": u["name"],
                "account_id": u["account_id"],
                "last_active": u["last_active"],
                "action": "skipped",
                "reason": "Service Account",
            })
            continue

        ok = suspend_user(u["account_id"], org_headers, dry_run=args.dry_run, session=session)
        if ok:
            if not args.dry_run:
                success(f"    Suspended: {u['email']}")
            else:
                print(f"    Would suspend: {u['email']}")
            suspended.append({
                "email": u["email"],
                "name": u["name"],
                "account_id": u["account_id"],
                "last_active": u["last_active"],
                "action": "suspended" if not args.dry_run else "would_suspend",
                "reason": "",
            })
            results.append(suspended[-1])
        else:
            failed += 1
            results.append({
                "email": u["email"],
                "name": u["name"],
                "account_id": u["account_id"],
                "last_active": u["last_active"],
                "action": "failed",
                "reason": "Suspension failed",
            })

    # Write results
    if results:
        out_path = get_available_filename(args.out)
        if out_path != args.out:
            warn(f"Output file {args.out} exists, writing to: {out_path}")
        write_results_csv(out_path, results)
        print(f"\nWrote results to: {out_path}")

    # Summary
    label = "Suspended" if not args.dry_run else "Would suspend"
    print(f"\nSummary:")
    print(f"  Org users loaded:          {len(all_users)}")
    print(f"  Cutoff date:               {args.before_date}")
    print(f"  Candidates:                {len(candidates)}")
    print(f"  {label:<15} {len(suspended)}")
    print(f"  Skipped (Service Account): {skipped_service_account}")
    if failed:
        print(f"  Failed:                    {failed}")


if __name__ == '__main__':
    main()
