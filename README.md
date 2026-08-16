AtlSuspendActiveUsers
=====================

Purpose
-------
Suspend all ACTIVE managed Atlassian Cloud accounts whose "Last active date" is before a cutoff date. The script supports dry-run, domain exclusions, and writing results to CSV.

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

- Specify org and output CSV:
  python AtlSuspendActiveUsers.py --org YOUR_ORG_ID -d 01.06.2024 --out suspended.csv

- Exclude domains from suspension:
  python AtlSuspendActiveUsers.py -d 01.01.2023 --exclude-domain example.com --exclude-domain example.com

Arguments (summary)
-------------------
- -d, --before-date    Cutoff date in DD.MM.YYYY. Accounts last active BEFORE this date are candidates.
- --org                Atlassian organization ID (or set ATLASSIAN_ORG env var)
- --exclude-domain     Exclude one or more domains from suspension (can be repeated)
- --include-never-active  Include accounts with no last_active value
- --out                Output CSV path (default: atl_suspended_users.csv)
- --dry-run            Do not perform suspensions; show what would be done

Behavior notes
--------------
- The script paginates the Atlassian Admin API to load all managed users.
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
