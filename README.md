# Soundings

**Weekly team activity digest across Gitea, GitHub, Jenkins, ArgoCD, Jira,
ZenDesk, and OpsGenie.**

Taking soundings means measuring the same water repeatedly to learn its shape.
Soundings does that for a team's week: it reads each system a team already
uses, reconciles the several usernames one person accumulates across them into
a single identity, and writes one markdown report and dashboard you can paste
into chat.

It is a description, not a scoreboard. The counts say what happened and where,
not who is doing well — activity volume is not performance, and the report is
most useful read as a prompt for questions rather than as an answer.

## File Structure

```text
soundings/
├── .env.example              # Environment variable template
├── team.example.json         # Example team roster (copy to team.json)
├── config.py                 # Configuration and constants
├── team.py                   # Team roster model and loader
├── utils.py                  # Utility functions (CSV export, git metric merge)
├── gitea_collector.py        # Gitea metrics collector
├── github_collector.py       # GitHub metrics collector (merged with Gitea)
├── jenkins_collector.py      # Jenkins metrics collector
├── argocd_collector.py       # ArgoCD metrics collector
├── jira_collectors.py        # Jira API and CSV collectors
├── zendesk_collector.py      # ZenDesk metrics collector
├── opsgenie_collector.py     # OpsGenie alert response collector
├── report_generator.py       # Visual report generation
├── main_collector.py         # Main orchestrator and CLI
├── requirements.txt          # Python dependencies
├── tests/                    # Dependency-free test scripts (see tests/README.md)
│   ├── run_all.py            # Runs every test_*.py; non-zero exit on failure
│   └── smoke_opsgenie.py     # Live OpsGenie check; needs real credentials
├── tools/
│   └── scan_identifiers.py   # Fails if a tracked file names a real person
├── .githooks/
│   └── pre-push              # Runs the scanner over every commit being pushed
└── reports/                  # Output directory (auto-created)
    ├── devops_metrics_runs_summary.md
    └── report_20250919_080000/   # one folder per run holds all outputs
        ├── devops_metrics_*.json
        ├── devops_metrics_*.csv
        ├── team_collaboration_report_*.md
        └── team_collaboration_dashboard.png
```

## Installation

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Configure environment variables:

   ```bash
   cp .env.example .env
   # Edit .env with your actual URLs, tokens, and credentials
   export $(grep -v '^#' .env | xargs)
   ```

   See `.env.example` for the full list of required and optional
   variables.

3. Define your team roster:

   ```bash
   cp team.example.json team.json
   # Edit team.json to list your team members and their usernames
   ```

   See [Team Roster](#team-roster) below. `team.json` is gitignored.

## Team Roster

Each person on the team is described once in `team.json` (path configurable via
`DEVOPS_METRICS_TEAM_FILE`, default `./team.json`). A person has a canonical
`id` (used internally as the key for all their metrics), a `display_name`
(shown in reports and charts), and an `aliases` map of **source → list of
usernames** they use in that source.

This lets you consolidate one person who appears under different usernames in
different systems — or even under several usernames within one system — into a
single set of metrics.

```json
[
  {
    "id": "leia.solo",
    "display_name": "Leia Solo",
    "aliases": {
      "default": ["leiasolo"],
      "jira": ["leiaskywalker"]
    }
  },
  {
    "id": "han.solo",
    "display_name": "Han Solo",
    "aliases": {
      "default": ["hsolo"],
      "gitea": ["hsolo", "jenosidanian"],
      "github": ["hsolo", "ijones"]
    }
  }
]
```

- **`default`** applies to any source without an explicit entry. In the example
  above, Leia is `leiasolo` in Gitea, Jenkins, ArgoCD, and ZenDesk, but
  `leiaskywalker` in Jira (e.g. after a name change).
- A source can list **multiple aliases**. Han commits as `hsolo` internally
  but as `jenosidanian` on repositories synced from GitHub into
  Gitea; both are attributed to him. Aliases may be usernames or commit emails.
- Valid source keys are: `gitea`, `github`, `jenkins`, `argocd`, `jira`,
  `zendesk`, `opsgenie`, plus `default`.
- OpsGenie usernames are usually full email addresses. A bare username in the
  roster is tried as-is first, then expanded with `OPSGENIE_EMAIL_DOMAIN`, so
  most rosters need no `opsgenie` entry at all.
- **`timezone`** (optional, IANA name like `America/Chicago`) sets what counts as
  after-hours for that person. Anyone without one falls back to
  `DEVOPS_METRICS_DEFAULT_TIMEZONE` (itself defaulting to UTC). Set this for a
  team spread across timezones — see
  [Out-of-hours buckets](#out-of-hours-buckets) below. An unrecognized name
  fails roster loading rather than silently reverting to UTC.

**Fallback:** if no team file exists, the roster falls back to the
comma-separated `DEVOPS_METRICS_TEAM_USERNAMES` env var, treating each username
as one person (one alias, used in every source). Existing setups keep working
without a `team.json`.

## Tests

```bash
python tests/run_all.py
```

Dependency-free scripts covering alert attribution, permission handling, the
out-of-hours window, dashboard layout, and end-to-end report generation. See
[tests/README.md](tests/README.md) for what each file covers and how to add more.

## Keeping Real Data Out of the Repository

Everything that names a real person stays local and gitignored: `team.json`, any
`.env`, the Jira export, and `reports/`. The committed tree carries placeholders
only — `team.example.json`, `.env.example`, and fixtures that use invented names
at `example.com`.

Fixtures are where this slips. A test written against your own roster reads as
ordinary code in review, so there is a scanner that derives its search terms from
the private files themselves and greps every tracked file for them:

```bash
python tools/scan_identifiers.py            # working tree
python tools/scan_identifiers.py --rev HEAD # a commit
python tools/scan_identifiers.py --list-terms
```

Wire it in as a pre-push hook, once per clone:

```bash
git config core.hooksPath .githooks
```

The hook scans **every commit being pushed**, not just the branch tip, because a
name removed by a later commit — or by an amend and a force-push — stays readable
on the forge at its original SHA. Catching it before the first push is the only
cheap fix; afterwards the options are deleting the repository or asking the forge
to garbage-collect it.

## Usage

### Basic Data Collection (Default)

```bash
# Collect raw JSON data only for last 90 days
python main_collector.py

# Custom time period (e.g., last 30 days)
python main_collector.py --days 30

# Generate both JSON and CSV formats
python main_collector.py --format json csv

# CSV only (great for spreadsheet analysis)
python main_collector.py --format csv
```

### Time Period Selection

```bash
# Last 7 days (weekly metrics)
python main_collector.py --days 7

# Last 30 days (monthly metrics)
python main_collector.py --days 30

# Last 180 days (6-month review)
python main_collector.py --days 180 --reports

# Custom period with full reports
python main_collector.py --days 45 --format json csv --reports

# Specific date range (ISO 8601 format, --from and --to must be used together)
python main_collector.py --from 2026-03-06T16:00-05:00 --to 2026-03-13T16:00-05:00

# Specific date range with reports
python main_collector.py --from 2026-03-01 --to 2026-03-14 --reports
```

### With Visual Reports (Optional)

```bash
# Raw data + visual reports (markdown + charts)
python main_collector.py --reports

# CSV data + visual reports for custom period
python main_collector.py --days 60 --format csv --reports

# Full package: JSON, CSV, and visual reports
python main_collector.py --format json csv --reports
```

### Advanced Examples

```bash
# Custom output directory
python main_collector.py -o /path/to/output --format csv

# Weekly collection with full reports
python main_collector.py --days 7 --reports

# Quick CSV export for spreadsheet analysis
python main_collector.py -f csv

# Quarterly review with all outputs
python main_collector.py --days 90 --format json csv --reports
```

## Command Line Options

| Option        | Short | Description                                  | Default |
| ------------- | ----- | -------------------------------------------- | ------- |
| `--days`      | -     | Number of days to collect                    | 90      |
| `--from`      | -     | Start of date range (ISO 8601). Use with --to | None    |
| `--to`        | -     | End of date range (ISO 8601). Use with --from | None    |
| `--format`    | `-f`  | Output format(s): json, csv, or both         | json    |
| `--reports`   | `-r`  | Generate visual reports                      | False   |
| `--output`    | `-o`  | Output directory for reports                 | reports |

> **Note:** `--from`/`--to` and `--days` are independent. When
> `--from`/`--to` are provided, the explicit date range is used and
> `--days` is ignored.

## Important Notes on Data Sources

### API-Based Collectors

Gitea, GitHub, Jenkins, ArgoCD, ZenDesk, and OpsGenie collectors
automatically filter data based on the `--days` parameter (or `--from`/`--to`
date range). When you specify `--days 30`, they will only collect
data from the last 30 days. When you specify an explicit date
range, they will collect data within that range.

The Gitea collector gathers commits, pull requests, code reviews,
and Actions workflow runs (including manually triggered runs via
`workflow_dispatch`). The Jenkins collector gathers build/deployment
metrics. Both Jenkins builds and Git Actions are included
alongside ArgoCD syncs in the GitOps Deployments section of
reports.

### GitHub Data Collection (Optional)

The GitHub collector is optional and is skipped unless `GITHUB_ORG`
(and `GITHUB_TOKEN`) are set. It discovers all repositories under the
configured github.com organization and gathers the same metrics as the
Gitea collector (commits, pull requests, code reviews, Actions runs).

GitHub and Gitea results are **merged into unified git totals**: a
person's commits, PRs, reviews, and Actions across both platforms are
summed into the single set of git charts, tables, and analysis. This
supports a migration where a select set of applications lives in GitHub
while the rest remain in Gitea. The raw per-source data is still kept in
the JSON output (`gitea_metrics` and `github_metrics`); the combined
view drives the markdown report, dashboard charts, and the CSV `git_*`
columns. Use a `github` alias entry in `team.json` to map each person's
GitHub login (and/or commit email) to their canonical id.

### ZenDesk Data Collection (Optional)

The ZenDesk collector is optional. It is automatically skipped
if the `ZENDESK_SUBDOMAIN` environment variable is not set.
When configured, it collects ticket activity, team member
comment counts, and customer satisfaction metrics (CSAT is
shown only when survey responses exist) for each team member.

### OpsGenie Data Collection (Optional)

The OpsGenie collector is optional. It is automatically skipped if the
`OPSGENIE_API_KEY` environment variable is not set. It answers a question no
other source can: **who actually responded to our alerts.**

Attribution comes from each alert's action log rather than the alert record, so
credit lands on the right person when one engineer acknowledges an alert and
another closes it. Per person, it collects:

- **Alert response** — alerts acknowledged, closed, noted on, and assigned
- **Escalations, in both directions** — see below
- **Response times** — average, median, and slowest time to acknowledge
- **On-call burden** — after-hours and weekend alerts handled
- **Priority mix** — the P1–P5 spread of alerts they touched

#### Out-of-hours buckets

An alert counts as **weekend** if it was created on a local Saturday or Sunday,
and **after-hours** if it was created on a weekday outside the working-day window
set by `DEVOPS_METRICS_BUSINESS_HOURS` (`HH:MM-HH:MM`, 24-hour clock, default
`08:00-18:00`). The start is inclusive and the end exclusive, so `09:00-17:30`
counts a 17:30 page as after-hours. Weekend takes precedence, so the two counts
never double-count one alert.

"Local" means **each person's own timezone**, taken from their `timezone` field in
`team.json`, falling back to `DEVOPS_METRICS_DEFAULT_TIMEZONE`. This matters for a
distributed team: with everyone measured against one fixed window, the same 19:00
page is after-hours for one person and mid-afternoon for another. Leaving the
default at UTC will misclassify a US-based team by 4–6 hours — a 2pm Eastern page
would be recorded as after-hours.

Both settings are validated at startup: an unrecognized timezone name or a
malformed window stops the run with an explanatory message instead of falling back
to a default that would quietly skew every count. Watch the `America/` prefix —
it is singular, so `Americas/New_York` is not a real zone.

These are workload signals for spotting who is carrying the pager, not
performance measures.

#### The two escalation counters

Escalation is tracked in both directions because the two tell opposite stories:

| Counter | CSV column | Meaning |
| ------- | ---------- | ------- |
| Escalated onward | `opsgenie_escalated_by` | Alerts this person handed off to someone else. Handoff behavior; a high count can mean alerts are routing to the wrong person first. |
| Received by escalation | `opsgenie_escalated_to` | Alerts that reached this person *because an earlier tier did not acknowledge in time*. A signal about the rotation, not about the individual. |

Read `escalated_to` as a property of the schedule rather than of the person who
received the page. A rising team-wide total usually means first-line coverage
needs attention.

#### Configuration gotchas

**`OPSGENIE_API_URL` is the REST API host, not the web portal.** The host you
log in to (`yourorg.app.opsgenie.com`) does not serve the API; only
`api.opsgenie.com` (or `api.eu.opsgenie.com`) does. Setting the portal host is
rejected by `validate_config()`, and if one reaches the collector anyway it is
corrected with a warning.

**The API key needs `Read` on an API integration** (Settings → Integrations →
API). That is the only permission required. The collector runs a preflight
against `/v2/alerts` before any per-member work, so a bad key or URL produces one
clear error instead of one warning per person.

A plain read-only key is typically **denied on `/v2/users`**, which the collector
handles by degrading rather than failing: user lookup is treated as optional
enrichment, and attribution falls back to matching alert-log actors against the
roster's usernames (each alias is tried bare and as `alias@OPSGENIE_EMAIL_DOMAIN`).
You will see one informational line saying so. Attribution still works — add an
explicit `opsgenie` alias for anyone whose OpsGenie login is not
`<alias>@<domain>`.

**A team-scoped integration only sees that team's alerts.** This shows up as zero
attributed alerts rather than an error, so the run logs an explicit warning when
nobody has any activity, naming both this and a username mismatch as the likely
causes.

#### Collection cost

Per-person attribution requires fetching the action log for each alert, so a
busy period costs roughly one request per alert on top of the searches. Alerts
per person per run are capped (`MAX_ALERTS_PER_MEMBER` in
`opsgenie_collector.py`, default 500); when the cap is hit the run logs a
warning naming the affected person, so a truncated count is never silent.
Narrow `--days` if you see one.

> **Note:** OpsGenie is on an Atlassian end-of-life path. This collector is
> deliberately self-contained so it can be replaced by a Jira Service
> Management equivalent without touching the report or chart layer — a new
> collector only needs to return the same metric shape.

### Jira Data Collection

#### Using Jira API

If configured with API credentials, the Jira collector will
automatically filter data based on the `--days` parameter or
`--from`/`--to` date range.

#### Using Jira CSV Export

**Important:** The CSV-based Jira collector analyzes ALL data
in the CSV file regardless of the `--days` parameter. You must
ensure your CSV export contains only the desired date range.

For example:

- If you run `python main_collector.py --days 30`
- But your Jira CSV contains 365 days of data
- The tool will analyze all 365 days from the CSV

**Best Practice:** When using CSV export, match your Jira
export date range to your intended `--days` parameter:

1. Export from Jira with your desired date filter
   (e.g., `created >= -30d`)
2. Save as `jira_export.csv` in the project directory
3. Run Soundings with matching days:
   `python main_collector.py --days 30`

## Key Features

### New Features

- **Alert Response Attribution**: OpsGenie ack/close/escalate credited to
  the person who took the action, with escalations tracked in both directions
- **Self-Sizing Dashboard**: The chart grid grows to fit whichever sources
  returned data — sources that are absent or empty no longer leave blank panels
- **Chat-Postable Outputs**: Both the dashboard and the report are sized to
  survive a chat post — see below
- **Flexible Time Periods**: Use `--days` for relative ranges
  or `--from`/`--to` for exact date ranges (ISO 8601)
- **Flexible Output Formats**: Choose JSON, CSV, or both
- **Optional Visual Reports**: Generate charts/markdown
  only when needed (faster default)
- **CSV Export**: Flattened data for spreadsheet analysis
- **Improved CLI**: Clear options for different use cases

### Performance Improvements

- **Faster Default**: JSON-only collection is much faster
- **On-Demand Visuals**: Generate charts only when requested
- **Better Organization**: Timestamped folders for comparison

### Usage Recommendations

- **Daily/Automated**: Use default (JSON only) for speed
- **Weekly Review**: `--days 7 --reports` for visual analysis
- **Monthly Review**: `--days 30 --reports`
- **Quarterly Review**: `--days 90 --format json csv --reports`
- **Spreadsheet Users**: Use `--format csv` for Excel/Sheets
- **Management**: Use `--reports` for full analysis

### Output Formats

**JSON**: Complete nested data structure, perfect for:

- API integration
- Custom analysis scripts
- Preserving all relationship data

**CSV**: Flattened tabular format, perfect for:

- Excel/Google Sheets analysis
- SQL database import
- Quick filtering and sorting
- Trend analysis over time

**Visual Reports**: Rich markdown + charts, perfect for:

- Management presentations
- Team retrospectives
- Identifying collaboration patterns
- Historical trending

#### Sharing outputs in chat

Chat systems cap both of these, and each new source pushes against the cap, so
both outputs are bounded by design:

- **Dashboard resolution** is derived from a pixel budget
  (`DASHBOARD_MAX_PIXELS` in `report_generator.py`, 25 MP) rather than fixed.
  Mattermost rejects an upload on total pixel count — 33.2 MP with the default
  `MaxImageResolution` — and a four-row grid at 300 dpi crossed that line. Extra
  panel rows now lower the render density instead of making the PNG unpostable.
  Raise the budget if your instance allows more; the grid layout is unaffected.
- **Individual highlights** list only the sources a person actually registered
  activity on, and only the counts that moved within each line. Someone who
  wrote no code and took no alerts no longer contributes a block of zeros to a
  post with a character limit (16,383 in Mattermost). The team summary table
  above the highlights still carries every source for everyone, so nothing is
  hidden — a member with no activity anywhere gets a one-line note instead of an
  empty section.
