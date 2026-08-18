#!/usr/bin/env python3
"""
Configuration and constants for Soundings
"""

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from team import load_team
from utils import parse_business_hours

# =============================================================================
# CONFIGURATION - Secrets are read from environment variables.
# See .env.example for the full list of required/optional variables.
# =============================================================================

# Team roster: a list of TeamMember objects, each of which may map several
# per-source usernames to one person. Loaded from DEVOPS_METRICS_TEAM_FILE
# (default ./team.json) with a fallback to DEVOPS_METRICS_TEAM_USERNAMES.
TEAM = load_team()

def _discover_labels(prefix: str) -> list:
    """Find instance labels by scanning for ``<PREFIX>_<LABEL>_URL`` variables.

    Used when ``<PREFIX>_INSTANCES`` is unset. Discovery rather than a fixed
    default matters: an environment that defines three instance URLs but no
    label list must not silently collect one of them and report success.
    Sorted so the instance order is deterministic across runs.
    """
    pattern = re.compile(rf'^{prefix}_(.+)_URL$')
    labels = []
    for name, value in os.environ.items():
        match = pattern.match(name)
        if match and value.strip():
            labels.append(match.group(1).lower())
    return sorted(labels)


def _load_instances(prefix: str, secret_keys: tuple) -> list:
    """Build a list of service instances from environment variables.

    Deployments differ in how many Jenkins/ArgoCD instances they run and what
    they call them, so the set is described by ``<PREFIX>_INSTANCES`` as a
    comma-separated list of labels. Each label ``L`` is then read from
    ``<PREFIX>_<L>_URL`` plus one variable per entry in *secret_keys*
    (e.g. ``TOKEN`` -> ``<PREFIX>_<L>_TOKEN``), falling back to the unsuffixed
    ``<PREFIX>_<KEY>`` so instances can share one credential.

    When ``<PREFIX>_INSTANCES`` is not set, the labels are discovered from the
    environment instead, so simply defining the URLs is enough. Set it
    explicitly only to restrict or order the set.

    A label with no URL set is skipped rather than contributing an instance
    that would fail every request.
    """
    configured = os.environ.get(f'{prefix}_INSTANCES')
    if configured is None:
        labels = _discover_labels(prefix)
    else:
        labels = [l.strip() for l in configured.split(',')]

    instances = []
    for label in labels:
        if not label:
            continue
        key = label.upper().replace('-', '_')
        base_url = os.environ.get(f'{prefix}_{key}_URL', '')
        if not base_url:
            continue
        instance = {
            'name': f'{prefix.lower()}-{label.lower()}',
            'label': label,
            'base_url': base_url,
        }
        for secret in secret_keys:
            instance[secret.lower()] = os.environ.get(
                f'{prefix}_{key}_{secret}', os.environ.get(f'{prefix}_{secret}', ''))
        instances.append(instance)
    return instances


CONFIG = {
    # Team Configuration
    'team': TEAM,
    # Canonical member ids, kept for any consumer that just needs the keys.
    'team_usernames': [m.id for m in TEAM],

    # Gitea Configuration
    'gitea': {
        'base_url': os.environ.get('GITEA_BASE_URL', ''),
        'token': os.environ.get('GITEA_TOKEN', ''),
        'org_name': os.environ.get('GITEA_ORG_NAME', ''),
        'include_user_repos': True,
    },

    # GitHub Configuration (optional - skipped if GITHUB_ORG is not set).
    # Discovers all repos under the org; used while a select set of apps lives
    # in GitHub and the rest remain in Gitea. Merged into unified git totals.
    'github': {
        'base_url': os.environ.get('GITHUB_API_URL', 'https://api.github.com'),
        'token': os.environ.get('GITHUB_TOKEN', ''),
        'org_name': os.environ.get('GITHUB_ORG', ''),
        'include_user_repos': False,
    },

    # Jenkins Configuration - one entry per JENKINS_<LABEL>_URL found, or per
    # label in JENKINS_INSTANCES when that is set. Credentials fall back to
    # JENKINS_USERNAME/JENKINS_PASSWORD when no per-instance override is set.
    'jenkins': {
        'instances': _load_instances('JENKINS', ('USERNAME', 'PASSWORD')),
    },

    # ArgoCD Configuration - one entry per ARGOCD_<LABEL>_URL found, or per
    # label in ARGOCD_INSTANCES when that is set.
    'argocd': {
        'instances': _load_instances('ARGOCD', ('TOKEN',)),
    },

    # Jira Configuration
    'jira': {
        'csv_path': os.environ.get('JIRA_CSV_PATH', './jira_export.csv'),
        'project_key': os.environ.get('JIRA_PROJECT_KEY', ''),
        'workflow_states': {
            'backlog': ['Backlog', 'Selected for Development', 'CAB Requires More Information', 'External CAB', 'Awaiting CAB Approval', 'Blocked'],
            'in_progress': ['In Progress', 'Internal Review', 'External Review'],
            'done': ['Done', 'Closed', 'Resolved']
        }
    },

    # ZenDesk Configuration (optional - skipped if subdomain is not set)
    'zendesk': {
        'subdomain': os.environ.get('ZENDESK_SUBDOMAIN', ''),
        'email': os.environ.get('ZENDESK_EMAIL', ''),
        'api_token': os.environ.get('ZENDESK_API_TOKEN', ''),
        'email_domain': os.environ.get('ZENDESK_EMAIL_DOMAIN', ''),
    },

    # OpsGenie Configuration (optional - skipped if no API key is set).
    # Provides alert response attribution (who acknowledged, closed, escalated),
    # which no other source can supply.
    'opsgenie': {
        'api_key': os.environ.get('OPSGENIE_API_KEY', ''),
        'base_url': os.environ.get('OPSGENIE_API_URL', 'https://api.opsgenie.com'),
        'email_domain': os.environ.get('OPSGENIE_EMAIL_DOMAIN',
                                       os.environ.get('ZENDESK_EMAIL_DOMAIN', '')),
        # Fallback for members whose team.json entry has no "timezone". Only
        # affects the after-hours/weekend buckets.
        'default_timezone': os.environ.get('DEVOPS_METRICS_DEFAULT_TIMEZONE', 'UTC'),
        # Working-day window, in each member's local time, as HH:MM-HH:MM.
        'business_hours': os.environ.get('DEVOPS_METRICS_BUSINESS_HOURS', '08:00-18:00'),
    }
}

def get_date_range(days=90, start=None, end=None):
    """Get date range for data collection.

    If *start* and *end* are provided they are used directly (must be
    timezone-aware datetimes).  Otherwise the range is computed as
    *days* back from now.
    """
    if start is not None and end is not None:
        return start, end
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)
    return start_date, end_date

def validate_config() -> bool:
    """Validate configuration before running"""
    required_keys = ['team', 'team_usernames', 'gitea', 'jenkins', 'argocd', 'jira']

    for key in required_keys:
        if key not in CONFIG:
            return False

    if not CONFIG['team']:
        print("Warning: team roster is empty. Configure team.json or "
              "DEVOPS_METRICS_TEAM_USERNAMES.")
        return False

    if not CONFIG['gitea'].get('base_url'):
        print("Warning: GITEA_BASE_URL environment variable is not set")
        return False

    if not CONFIG['gitea'].get('org_name'):
        print("Warning: GITEA_ORG_NAME environment variable is not set")
        return False

    if not CONFIG['gitea'].get('token'):
        print("Warning: GITEA_TOKEN environment variable is not set")
        return False

    # Check Jenkins instances
    if not CONFIG['jenkins'].get('instances'):
        print("Warning: no Jenkins instances configured. Set JENKINS_INSTANCES to a "
              "comma-separated list of labels and JENKINS_<LABEL>_URL for each.")
        return False

    for inst in CONFIG['jenkins']['instances']:
        if not inst.get('username') or not inst.get('password'):
            key = inst['label'].upper().replace('-', '_')
            print(f"Warning: Jenkins credentials missing for {inst['name']}. "
                  f"Set JENKINS_USERNAME/JENKINS_PASSWORD or "
                  f"JENKINS_{key}_USERNAME/JENKINS_{key}_PASSWORD.")
            return False

    # Check ArgoCD instances
    if not CONFIG['argocd'].get('instances'):
        print("Warning: no ArgoCD instances configured. Set ARGOCD_INSTANCES to a "
              "comma-separated list of labels and ARGOCD_<LABEL>_URL for each.")
        return False

    for inst in CONFIG['argocd']['instances']:
        if not inst.get('token'):
            key = inst['label'].upper().replace('-', '_')
            print(f"Warning: ArgoCD token missing for {inst['name']}. "
                  f"Set ARGOCD_{key}_TOKEN env var.")
            return False

    # Check GitHub config (only if an org is set — GitHub is optional)
    github = CONFIG.get('github', {})
    if github.get('org_name'):
        if not github.get('token'):
            print("Warning: GITHUB_TOKEN is required when GITHUB_ORG is set")
            return False

    # Check ZenDesk config (only if subdomain is set)
    zendesk = CONFIG.get('zendesk', {})
    if zendesk.get('subdomain'):
        if not zendesk.get('email'):
            print("Warning: ZENDESK_EMAIL is required when ZENDESK_SUBDOMAIN is set")
            return False
        if not zendesk.get('api_token'):
            print("Warning: ZENDESK_API_TOKEN is required when ZENDESK_SUBDOMAIN is set")
            return False

    # Check OpsGenie config (only if an API key is set — OpsGenie is optional).
    opsgenie = CONFIG.get('opsgenie', {})
    if opsgenie.get('api_key'):
        base_url = opsgenie.get('base_url', '')
        if not base_url:
            print("Warning: OPSGENIE_API_URL must not be empty when OPSGENIE_API_KEY is set")
            return False
        # The web portal host is the one people copy out of a browser, but it
        # does not serve the REST API. Say so here rather than letting every
        # user lookup fail with no request having been made.
        if 'app.opsgenie.com' in base_url:
            print(f"Warning: OPSGENIE_API_URL is set to the OpsGenie web portal "
                  f"({base_url}), which does not serve the REST API. Use "
                  f"https://api.opsgenie.com (or https://api.eu.opsgenie.com for "
                  f"EU-hosted accounts).")
            return False

    # Validate the out-of-hours settings whether or not OpsGenie is enabled, and
    # fail rather than warn: a bad timezone name would otherwise fall back to UTC
    # and quietly misclassify every after-hours count.
    default_tz = CONFIG.get('opsgenie', {}).get('default_timezone', 'UTC')
    try:
        ZoneInfo(default_tz)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"Warning: DEVOPS_METRICS_DEFAULT_TIMEZONE={default_tz!r} is not a known "
              f"IANA timezone name. Note the singular 'America/...' prefix, e.g. "
              f"America/New_York or America/Chicago.")
        return False

    try:
        parse_business_hours(CONFIG.get('opsgenie', {}).get('business_hours'))
    except ValueError as e:
        print(f"Warning: DEVOPS_METRICS_BUSINESS_HOURS is invalid: {e}")
        return False

    return True
