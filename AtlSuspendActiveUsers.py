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
  python AtlUserSuspend.py -d 01.01.2023 --exclude-domain idera.com --exclude-domain embarcadero.com
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
import requests
from requests.exceptions import RequestException

BASE_URL = "https://api.atlassian.com/admin/v1"
USER_MGMT_URL = "https://api.atlassian.com/users"

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


def get_auth_header():
    token = os.getenv("ATLASSIAN_TOKEN")
    if not token:
        print("ATLASSIAN_TOKEN env var not set", file=sys.stderr)
        sys.exit(2)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def request_with_retries(method, url, headers=None, params=None, json=None, timeout=20, max_retries=4):
    """Perform HTTP request with basic retry/backoff on 429 and 5xx errors.

    Returns requests.Response or raises RequestException after retries.
    """
    attempt = 0
    last_exc = None
    last_resp = None
    while attempt < max_retries:
        attempt += 1
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json, timeout=timeout)
        except RequestException as e:
            last_exc = e
            backoff = 1 * (2 ** (attempt - 1))
            warn(f"Request error (attempt {attempt}/{max_retries}): {e}. Retrying in {backoff}s")
            time.sleep(backoff)
            continue

        if resp.status_code == 429:
            retry_after = resp.headers.get('Retry-After')
            try:
                wait = int(retry_after) if retry_after else 1 * (2 ** (attempt - 1))
            except Exception:
                wait = 1 * (2 ** (attempt - 1))
            warn(f"Rate limited (429). Waiting {wait}s before retry (attempt {attempt}/{max_retries})")
            time.sleep(wait)
            last_resp = resp
            continue
        if 500 <= resp.status_code < 600:
            backoff = 1 * (2 ** (attempt - 1))
            warn(f"Server error {resp.status_code} (attempt {attempt}/{max_retries}). Retrying in {backoff}s")
            time.sleep(backoff)
            last_resp = resp
            continue

        return resp

    # exhausted retries
    if last_exc:
        raise last_exc
    if last_resp is not None:
        return last_resp
    raise RequestException("Max retries exhausted with no response")


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


def load_all_org_users(org_id, headers):
    """Paginate all managed users in the org.

    Returns list of dicts with email, account_id, name, account_status, last_active, product_access.
    """
    url = f"{BASE_URL}/orgs/{org_id}/users"
    all_users = []
    page = 0

    def _get(u):
        return request_with_retries('GET', u, headers=headers, timeout=30)

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
                    "has_product_access": len(product_access) > 0,
                })

        next_link = data.get("links", {}).get("next")
        url = next_link if next_link else None

    print(f"  Loaded {len(all_users)} users across {page} pages.")
    return all_users

def suspend_user(account_id, headers, dry_run=False):
    """Suspend (disable) a user via the User Management lifecycle API."""
    if not account_id:
        warn("Cannot suspend user without account_id")
        return False

    url = f"{USER_MGMT_URL}/{account_id}/manage/lifecycle/disable"
    if dry_run:
        print(f"  DRY RUN: POST {url}")
        return True

    try:
        resp = request_with_retries('POST', url, headers=headers, timeout=20, max_retries=4)
    except RequestException as e:
        error(f"  Suspend request failed for {account_id}: {e}")
        return False

    if resp.status_code in (200, 204):
        return True

    error(f"  Suspend failed for {account_id}: {resp.status_code} {resp.text}")
    return False


def domain_of(email: str):
    return email.split('@', 1)[1].lower() if email and '@' in email else None


def write_results_csv(path, suspended_users):
    """Write suspended accounts to CSV with headers."""
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["email", "name", "account_id", "last_active", "status"])
        for u in suspended_users:
            w.writerow([u["email"], u["name"], u["account_id"], u["last_active"] or "never", u["status"]])


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
            "  python AtlUserSuspend.py -d 01.01.2023 --exclude-domain idera.com\n"
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
    org_id = args.org or os.getenv("ATLASSIAN_ORG")
    if not org_id:
        print("Error: Org ID required. Provide --org or set ATLASSIAN_ORG env var.", file=sys.stderr)
        sys.exit(1)
    if not args.org and os.getenv("ATLASSIAN_ORG"):
        warn("--org not provided, using ATLASSIAN_ORG from environment")

    headers = get_auth_header()

    # Load ALL org users
    print(f"Loading all managed users from org {org_id}...")
    all_users = load_all_org_users(org_id, headers)

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

    if not candidates:
        print("\nNo accounts to suspend.")
        return

    # Suspend
    suspended = []
    failed = 0

    for i, u in enumerate(candidates, 1):
        print(f"  [{i}/{len(candidates)}] Suspending {u['email']} (last active: {u['last_active'] or 'never'})...")
        ok = suspend_user(u["account_id"], headers, dry_run=args.dry_run)
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
                "status": "suspended" if not args.dry_run else "would_suspend",
            })
        else:
            failed += 1

    # Write results
    if suspended:
        out_path = get_available_filename(args.out)
        if out_path != args.out:
            warn(f"Output file {args.out} exists, writing to: {out_path}")
        write_results_csv(out_path, suspended)
        print(f"\nWrote results to: {out_path}")

    # Summary
    label = "Suspended" if not args.dry_run else "Would suspend"
    print(f"\nSummary:")
    print(f"  Org users loaded:          {len(all_users)}")
    print(f"  Cutoff date:               {args.before_date}")
    print(f"  Candidates:                {len(candidates)}")
    print(f"  {label}:          {len(suspended)}")
    if failed:
        print(f"  Failed:                    {failed}")


if __name__ == '__main__':
    main()
