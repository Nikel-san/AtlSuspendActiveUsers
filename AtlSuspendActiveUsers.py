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


def search_site_user(email, headers, session=None):
    """Find a site user by exact, case-insensitive email address."""
    email = email.strip()
    url = f"{get_site_base_url()}/rest/api/3/user/search"
    try:
        resp = request_with_retries(
            session,
            "GET",
            url,
            headers=headers,
            params={"query": email, "maxResults": 50},
            timeout=30,
        )
        resp.raise_for_status()
        users = resp.json()
    except (RequestException, ValueError) as exc:
        warn(f"Could not search Jira site users for {email}: {exc}")
        return None

    if not isinstance(users, list):
        return None
    for user in users:
        email_address = (user.get("emailAddress") or "").strip()
        if email_address and email_address.lower() != email.lower():
            continue
        return {
            "email": email_address or email,
            "account_id": user.get("accountId"),
            "name": user.get("displayName", ""),
            "account_status": "active" if user.get("active") else "inactive",
            "account_type": user.get("accountType", ""),
            "last_active": None,
            "product_access": [],
        }
    return None


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
    """Paginate all organization users via the Admin v1 users endpoint.

    Returns list of dicts with email, account_id, name, account_status, last_active, product_access.
    """
    url = f"{ADMIN_API_BASE_URL}/orgs/{org_id}/users"
    all_users = []
    page = 0

    while url:
        page += 1
        print(f"  Loading organization users page {page}...", end="\r")
        resp = request_with_retries(
            session,
            "GET",
            url,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for u in data.get("data", []):
            email = u.get("email") or u.get("emailAddress")
            if not email:
                continue
            product_access = u.get("product_access") or u.get("productAccess") or []
            last_active_str = u.get("last_active") or u.get("lastActive")
            if not last_active_str and product_access:
                product_dates = [
                    p.get("last_active") or p.get("lastActive")
                    for p in product_access
                    if p.get("last_active") or p.get("lastActive")
                ]
                if product_dates:
                    last_active_str = max(
                        product_dates,
                        key=lambda value: parse_last_active(value) or datetime.min.replace(tzinfo=timezone.utc),
                    )

            if "is_managed" in u:
                account_type = "managed" if u["is_managed"] else "external"
            elif "managed" in u:
                account_type = "managed" if u["managed"] else "external"
            else:
                account_type = u.get("account_type") or u.get("accountType") or ""

            all_users.append({
                "email": email,
                "account_id": u.get("account_id") or u.get("accountId"),
                "name": u.get("name") or u.get("displayName", ""),
                "account_status": u.get("account_status") or u.get("accountStatus") or u.get("status", ""),
                "account_type": account_type,
                "last_active": last_active_str,
                "product_access": product_access,
            })

        next_link = data.get("links", {}).get("next")
        url = next_link if next_link else None

    print(f"  Loaded {len(all_users)} users across {page} pages.")
    return all_users


def load_all_site_users(headers, session=None):
    """Paginate Jira site users to discover accounts absent from the org list."""
    url = f"{get_site_base_url()}/rest/api/3/users/search"
    all_users = []
    start_at = 0
    max_results = 1000

    while True:
        try:
            resp = request_with_retries(
                session,
                "GET",
                url,
                headers=headers,
                params={"startAt": start_at, "maxResults": max_results},
                timeout=30,
            )
            resp.raise_for_status()
            users = resp.json()
        except (RequestException, ValueError) as exc:
            warn(f"Could not load Jira site users: {exc}")
            break

        if not isinstance(users, list):
            break

        for user in users:
            email = user.get("emailAddress") or user.get("email")
            account_id = user.get("accountId") or user.get("account_id")
            if not email or not account_id:
                continue
            account_type = user.get("accountType") or user.get("account_type")
            all_users.append({
                "email": email,
                "account_id": account_id,
                "name": user.get("displayName") or user.get("name", ""),
                "account_status": "active" if user.get("active") else "inactive",
                "account_type": "managed" if account_type == "atlassian" else "external",
                "last_active": None,
                "product_access": [],
            })

        if len(users) < max_results:
            break
        start_at += len(users)

    return all_users


def merge_org_users(*user_lists):
    """Merge discovery results while retaining one record per organization user."""
    merged = {}
    for users in user_lists:
        for user in users:
            key = user.get("account_id") or user.get("email", "").lower()
            if not key:
                continue
            existing = merged.get(key, {})
            merged[key] = {
                "last_active": None,
                **existing,
                **{field: value for field, value in user.items() if value not in (None, "", [])},
            }
    return list(merged.values())

def normalize_account_type(account_type):
    normalized = (account_type or "").strip().lower().replace("_", "-")
    if normalized in {"external", "unmanaged", "unmanaged-account", "external-account"}:
        return "external"
    if normalized in {"managed", "managed-account"}:
        return "managed"
    if normalized == "atlassian":
        return "managed"
    return "unknown"


def suspend_user(org_id, user, headers, dry_run=False, session=None):
    """Suspend a managed account or suspend external organization access."""
    account_id = user.get("account_id")
    if not account_id:
        warn("Cannot suspend user without account_id")
        return False

    if normalize_account_type(user.get("account_type")) != "external":
        url = f"{USER_MGMT_API_BASE_URL}/{account_id}/manage/lifecycle/disable"
    else:
        url = f"{ADMIN_API_BASE_URL}/orgs/{org_id}/directory/users/{account_id}/suspend-access"
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
    """Write suspension outcomes to CSV, including skipped users."""
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["email", "name", "account_id", "account_type", "last_active", "action", "reason"])
        for u in results:
            w.writerow([
                u["email"],
                u["name"],
                u["account_id"],
                u["account_type"],
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


def load_email_file(path):
    """Load non-empty email values from a UTF-8 or UTF-8-BOM CSV file."""
    with open(path, "r", encoding="utf-8-sig", newline="") as source:
        return [row[0].strip() for row in csv.reader(source) if row and row[0].strip()]


def result_for(user, action, reason=""):
    return {
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "account_id": user.get("account_id", ""),
        "account_type": normalize_account_type(user.get("account_type")),
        "last_active": user.get("last_active"),
        "action": action,
        "reason": reason,
    }


def main():
    p = argparse.ArgumentParser(
        description="AtlUserSuspend: suspend active Atlassian accounts by date or CSV input",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python AtlUserSuspend.py -d 01.01.2023\n"
            "  python AtlUserSuspend.py -d 01.01.2023 --dry-run\n"
            "  python AtlUserSuspend.py -d 01.06.2024 --out suspended.csv\n"
            "  python AtlUserSuspend.py -d 01.01.2023 --exclude-domain example.com\n"
            "  python AtlUserSuspend.py -d 01.01.2023 --include-never-active\n"
            "  python AtlUserSuspend.py --file users.csv --dry-run\n"
        )
    )
    p.add_argument('--org', required=False,
                   help='Organization ID (or set ATLASSIAN_ORG env var)')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('-d', '--before-date',
                   help='Cutoff date in DD.MM.YYYY format. Accounts last active BEFORE this date will be suspended.')
    mode.add_argument('-f', '--file',
                   help='CSV file containing one email address per row, without a header')
    p.add_argument('--exclude-domain', action='append', default=[],
                   help='Domain(s) to exclude from suspension (can be specified multiple times)')
    p.add_argument('--include-never-active', action='store_true',
                   help='Also suspend accounts that have never been active (no last_active date)')
    p.add_argument('--out', required=False, default='atl_suspended_users.csv',
                   help='Output CSV file path (default: atl_suspended_users.csv)')
    p.add_argument('--dry-run', action='store_true',
                   help='Preview suspensions without performing them')
    args = p.parse_args()

    cutoff_date = None
    if args.before_date:
        try:
            cutoff_date = datetime.strptime(args.before_date, "%d.%m.%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Error: Invalid date format '{args.before_date}'. Expected DD.MM.YYYY (e.g. 01.01.2023)", file=sys.stderr)
            sys.exit(2)

    requested_emails = []
    if args.file:
        try:
            requested_emails = load_email_file(args.file)
        except (OSError, csv.Error) as exc:
            print(f"Error: Could not read CSV file '{args.file}': {exc}", file=sys.stderr)
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

    # File mode searches the Jira site directly; date mode combines org and site discovery.
    if args.file:
        all_users = []
    else:
        print(f"Loading all organization users from org {org_id}...")
        org_users = load_all_org_users(org_id, org_headers, session=session)
        site_users = load_all_site_users(site_headers, session=session)
        all_users = merge_org_users(org_users, site_users)
        if not all_users:
            print("No users found in org.")
            return

    # Filter: active accounts selected by date or by email.
    exclude_domains = set(d.lower().strip() for d in args.exclude_domain)
    candidates = []
    skipped_inactive = 0
    skipped_domain = 0
    skipped_recent = 0
    skipped_never_active = 0
    skipped_not_found = 0
    skipped_service_account = 0
    results = []

    if args.file:
        for email in requested_emails:
            user = search_site_user(email, site_headers, session=session)
            if not user:
                skipped_not_found += 1
                results.append(result_for({"email": email}, "skipped", "User not found"))
                continue
            if (user.get("account_status") or "").lower() != "active":
                skipped_inactive += 1
                results.append(result_for(user, "skipped", "Already inactive"))
                continue
            if exclude_domains and domain_of(user["email"]) in exclude_domains:
                skipped_domain += 1
                results.append(result_for(user, "skipped", "Excluded domain"))
                continue
            candidates.append(user)

    for u in all_users if args.before_date else []:
        # only suspend active accounts (normalize casing and handle missing key)
        if (u.get("account_status") or "").lower() != "active":
            skipped_inactive += 1
            continue

        # exclude specific domains
        if exclude_domains and domain_of(u["email"]) in exclude_domains:
            skipped_domain += 1
            continue

        last_active_dt = parse_last_active(u.get("last_active"))

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
    mode_description = f"emails from {args.file}" if args.file else f"last active before {args.before_date}"
    print(f"\nSuspend mode [{action}]: suspending active accounts selected by {mode_description}")
    if exclude_domains:
        print(f"  Excluding domains: {', '.join(sorted(exclude_domains))}")
    print(f"  Candidates for suspension: {len(candidates)}")
    print(f"  Skipped (already inactive): {skipped_inactive}")
    print(f"  Skipped (excluded domain):  {skipped_domain}")
    print(f"  Skipped (user not found):   {skipped_not_found}")
    print(f"  Skipped (active after cutoff): {skipped_recent}")
    print(f"  Skipped (never active, not included): {skipped_never_active}")
    print(f"  Skipped (Service Account): {skipped_service_account}")

    if not candidates and not results:
        print("\nNo accounts to suspend.")
        return

    if not candidates:
        print("\nNo accounts to suspend.")
        out_path = get_available_filename(args.out)
        if out_path != args.out:
            warn(f"Output file {args.out} exists, writing to: {out_path}")
        write_results_csv(out_path, results)
        print(f"Wrote results to: {out_path}")
        return

    # Suspend
    suspended = []
    failed = 0

    for i, u in enumerate(candidates, 1):
        print(f"  [{i}/{len(candidates)}] Suspending {u['email']} (last active: {u['last_active'] or 'never'})...")
        if u.get("account_id"):
            job_title = get_user_profile(u["account_id"], org_headers, session=session)
            if job_title is None:
                warn(f"  Could not verify profile for {u['email']} — skipping")
                results.append(result_for(u, "skipped", "Profile unavailable"))
                continue
        else:
            job_title = ""

        if is_service_account_job_title(job_title):
            skipped_service_account += 1
            print(f"{YELLOW}    Skipped: {u['email']} — Service Account — skipped{RESET}")
            results.append(result_for(u, "skipped", "Service Account"))
            continue

        ok = suspend_user(org_id, u, org_headers, dry_run=args.dry_run, session=session)
        if ok:
            if not args.dry_run:
                success(f"    Suspended: {u['email']}")
            else:
                print(f"    Would suspend: {u['email']}")
            suspended.append(result_for(u, "suspended" if not args.dry_run else "would_suspend"))
            results.append(suspended[-1])
        else:
            failed += 1
            results.append(result_for(u, "failed", "Suspension failed"))

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
    print(f"  Selection:                 {mode_description}")
    print(f"  Candidates:                {len(candidates)}")
    print(f"  {label:<15} {len(suspended)}")
    print(f"  Skipped (Service Account): {skipped_service_account}")
    if failed:
        print(f"  Failed:                    {failed}")


if __name__ == '__main__':
    main()
