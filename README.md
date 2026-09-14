AtlSuspendActiveUsers
=====================

Purpose
-------
Suspend active Atlassian Cloud accounts selected by a last-active cutoff date or by a single-column email CSV. Managed accounts use the lifecycle API; external accounts use the organization-level access suspension action. The script supports dry-run, domain exclusions, and writing results to CSV.

Prerequisites
-------------
- Python 3.8+ (3.11 recommended)
- requests library (pip install requests)
- ATLASSIAN_TOKEN environment variable containing a valid Atlassian bearer token for the org admin API (`api.atlassian.com`)
- JIRA_EMAIL and JIRA_PAT environment variables for site-level Jira/Confluence API auth when needed
- Org ID provided via --org or ATLASSIAN_ORG environment variable

Quick examples
--------------
- Suspend accounts last active before 01.01.2023 (live):
  python AtlSuspendActiveUsers.py -d 01.01.2023

- Dry-run (preview only):
  python AtlSuspendActiveUsers.py -d 01.01.2023 --dry-run

- Suspend users from a UTF-8 CSV with one email per row and no header:
  python AtlSuspendActiveUsers.py --file users.csv

- Preview CSV suspensions:
  python AtlSuspendActiveUsers.py -f users.csv --dry-run

- Specify org and output CSV:
  python AtlSuspendActiveUsers.py --org YOUR_ORG_ID -d 01.06.2024 --out suspended.csv

- Exclude domains from suspension:
  python AtlSuspendActiveUsers.py -d 01.01.2023 --exclude-domain example.com --exclude-domain example.com

Arguments (summary)
-------------------
- -d, --before-date    Cutoff date in DD.MM.YYYY. Accounts last active BEFORE this date are candidates.
- -f, --file           CSV containing one email address per row, without a header. Mutually exclusive with --before-date.
- --org                Atlassian organization ID (or set ATLASSIAN_ORG env var)
- --exclude-domain     Exclude one or more domains from suspension (can be repeated)
- --include-never-active  Include accounts with no last_active value
- --out                Output CSV path (default: atl_suspended_users.csv)
- --dry-run            Do not perform suspensions; show what would be done

Behavior notes
--------------
- Date mode paginates the Atlassian Admin API to load all organization users. File mode skips that pagination and looks up each email through the Jira Site User Search API (`GET /rest/api/3/user/search?query=...`).
- File mode matches a populated `emailAddress` exactly, case-insensitively, and accepts a result with a privacy-redacted email address because the search query is the requested email. It uses the returned account ID, display name, active status, and account type.
- CSV input accepts UTF-8 and UTF-8-BOM files, strips whitespace, and ignores blank rows.
- Users not found in the organization are recorded as skipped with reason `User not found`.
- Managed accounts use User Management lifecycle disable. External/unmanaged accounts, including Jira users reported with `accountType="atlassian"`, use organization-level suspend access, avoiding the lifecycle API 403 response.
- Output CSV columns are `email`, `name`, `account_id`, `account_type`, `last_active`, `action`, and `reason`.
- When the top-level last_active is missing, the script falls back to the most recent product-level last_active.
- Account status comparisons are case-insensitive.
- Before suspension, the script calls the user profile endpoint and checks the job title. If the title contains "Service Account" (case-insensitive), the user is skipped and recorded in the CSV with action="skipped" and reason="Service Account".
- If the profile endpoint cannot be fetched, the user is skipped with action="skipped" and reason="Profile unavailable" instead of being suspended.
- HTTP requests use a retry-enabled session for 429 and 5xx responses.
- The script prints ANSI-colored messages; on Windows you may want to enable VT100 support or use colorama.

Service account exclusion
-------------------------
- Users whose job title contains "Service Account" are automatically skipped.
- Matching is case-insensitive, so "service account", "Service Account", and "SERVICE ACCOUNT" are all treated the same.
- Example output:
  - `Skipped: user@example.com — Service Account — skipped`

License
-------
No license specified. Use and modify as needed.
