#!/usr/bin/env python3
"""
OpsGenie Metrics Collector

Answers "who actually responded to our alerts?" — a question the other
collectors cannot, because alert response happens entirely inside OpsGenie.

Attribution comes from each alert's *log* (``/v2/alerts/{id}/logs``) rather than
the alert record itself.  The alert record only carries the final ``owner`` and
a single ``report.acknowledgedBy``/``report.closedBy``, which loses the common
case of one person acknowledging and someone else closing.  The logs record
every action with the user who took it, so credit lands on the right person.

Escalations are tracked in *both* directions, because they tell opposite
stories:

* ``escalated_by`` - alerts this person handed off to someone else.  Handoff
  behaviour; high counts may mean the alert routed to the wrong person first.
* ``escalated_to`` - alerts that reached this person through an escalation
  rule, usually because nobody acknowledged in time.  A signal about the
  rotation, not about the individual.
"""

import re
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Set

from utils import (logger, create_session, classify_work_time, resolve_timezone,
                   parse_business_hours)
from team import TeamMember

# Maximum alerts fetched per member per run. OpsGenie's search endpoint pages at
# 100 and each alert costs an extra request for its logs, so this bounds a busy
# period from turning into thousands of calls. Exceeding it is logged, never
# silent — a truncated count that looks complete is worse than a loud warning.
MAX_ALERTS_PER_MEMBER = 500

# OpsGenie search pages via offset/limit; 100 is the API maximum.
SEARCH_PAGE_SIZE = 100

DEFAULT_API_URL = 'https://api.opsgenie.com'
EU_API_URL = 'https://api.eu.opsgenie.com'


class OpsGenieCollector:
    def __init__(self, config: Dict[str, Any]):
        self.api_key = config['api_key']
        self.base_url = self._normalize_base_url(config.get('base_url') or DEFAULT_API_URL)
        self.email_domain = config.get('email_domain', '')
        # Fallback timezone for members whose roster entry does not name one.
        # Out-of-hours counts are meaningless without the right local time.
        self.default_timezone = config.get('default_timezone') or 'UTC'
        # Working-day window applied in each member's local time.
        self.business_hours = parse_business_hours(config.get('business_hours'))
        self.session = create_session()
        self.headers = {'Authorization': f'GenieKey {self.api_key}'}
        # username -> resolved OpsGenie user identity (id + username), or None
        self._user_cache: Dict[str, Optional[Dict[str, str]]] = {}
        # alert id -> logs, shared across members so a shared alert is fetched once
        self._logs_cache: Dict[str, List[Dict[str, Any]]] = {}
        # Flipped off the first time /v2/users is denied, so a key without user
        # read permission does not produce one failed lookup per team member.
        self._user_lookup_available = True

    @staticmethod
    def _normalize_base_url(raw: str) -> str:
        """Coerce a configured URL into the REST API base URL.

        The web portal host (``<org>.app.opsgenie.com``) is the value people
        naturally copy out of a browser, but it does not serve the REST API —
        only ``api.opsgenie.com`` does. Silently accepting it produced a run
        where every user lookup "failed" without a single request being made, so
        the portal host is corrected here and the correction is logged loudly.
        """
        url = raw.strip().rstrip('/')

        if '://' not in url:
            url = f"https://{url}"
            logger.warning(f"OpsGenie API URL had no scheme; assuming {url}")

        host = url.split('://', 1)[1].split('/', 1)[0].lower()

        if host.endswith('.app.eu.opsgenie.com') or host == 'app.eu.opsgenie.com':
            logger.warning(
                f"OPSGENIE_API_URL points at the web portal ({host}), which does not "
                f"serve the REST API. Using {EU_API_URL} instead — update your .env.")
            return EU_API_URL

        if host.endswith('.app.opsgenie.com') or host == 'app.opsgenie.com':
            logger.warning(
                f"OPSGENIE_API_URL points at the web portal ({host}), which does not "
                f"serve the REST API. Using {DEFAULT_API_URL} instead — update your .env.")
            return DEFAULT_API_URL

        return url

    def verify_connection(self) -> bool:
        """Check reachability and *alert read* access before per-member work.

        Deliberately probes ``/v2/alerts`` rather than ``/v2/account``: account
        info requires Configuration Access, which a read-only API key does not
        have, so a perfectly good key 403s there. Alert read is the one
        capability this collector actually needs, so that is what gets checked.
        """
        url = f"{self.base_url}/v2/alerts"
        try:
            response = self.session.get(url, headers=self.headers,
                                        params={'limit': 1}, timeout=15)
        except Exception as e:
            logger.error(f"OpsGenie is unreachable at {self.base_url}: {e}")
            return False

        if response.status_code == 200:
            logger.info("OpsGenie connection verified (alert read access confirmed)")
            return True

        if response.status_code in (401, 403):
            logger.error(
                f"OpsGenie denied alert read access ({response.status_code}). The key "
                f"needs 'Read' enabled on an API integration "
                f"(Settings -> Integrations -> API), and the integration must not be "
                f"restricted to a team that excludes these alerts.")
        else:
            logger.error(
                f"OpsGenie preflight failed at {url}: {response.status_code} "
                f"{response.text[:200]}")
        return False

    def collect_team_metrics(self, members: List[TeamMember],
                            start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Collect OpsGenie metrics using the two-phase pattern.

        Phase 1: Fetch alerts and their logs from the OpsGenie API.
        Phase 2: Analyze the fetched data into metrics.

        A member may map to several OpsGenie identities; actions across all of
        their usernames are combined under their canonical id.
        """
        team_metrics = {}

        # Fail once, clearly, rather than once per member.
        if not self.verify_connection():
            logger.error("Skipping OpsGenie collection; see the error above.")
            return {member.id: self._empty_metrics() for member in members}

        total_touched = 0

        for member in members:
            logger.info(f"Collecting OpsGenie metrics for {member.id}")

            # Phase 1: work out which OpsGenie identities belong to this person
            identities = self._identities_for(member)

            # Match log entries by username and by id — OpsGenie stows the actor
            # in `owner` (a username) on logs but uses ids elsewhere.
            actor_keys = set()
            for identity in identities:
                actor_keys.add(identity['username'].lower())
                if identity.get('id'):
                    actor_keys.add(identity['id'].lower())

            alerts = self._fetch_alerts(identities, start_date, end_date)
            alert_logs = {alert['id']: self._fetch_alert_logs(alert['id'])
                          for alert in alerts if alert.get('id')}

            # Phase 2: Analyze. Out-of-hours buckets use this person's own
            # timezone, so a distributed team is measured against local nights
            # and weekends rather than one shared window.
            member_tz = resolve_timezone(member.timezone or self.default_timezone)
            metrics = self._analyze_metrics(alerts, alert_logs, actor_keys, member_tz)
            total_touched += metrics['alert_response']['alerts_involved']
            team_metrics[member.id] = metrics

        if total_touched == 0:
            logger.warning(
                "OpsGenie returned no attributable alert activity for anyone. If the "
                "team was on call during this window, the likely causes are: the API "
                "integration is scoped to a team that does not own these alerts, or "
                "the roster usernames do not match OpsGenie logins (expected form: "
                f"<alias>@{self.email_domain}).")

        return team_metrics

    def _identities_for(self, member: TeamMember) -> List[Dict[str, str]]:
        """Best-effort OpsGenie identities for one person.

        Looking a user up via ``/v2/users`` is *enrichment*, not a requirement:
        alert logs name the actor by username, so the roster's own candidate
        usernames work as match keys on their own. This matters because an API
        key scoped to alert reads — the common case — is denied on the users
        endpoint, and losing attribution entirely over a missing lookup
        permission would be the wrong trade.
        """
        identities: List[Dict[str, str]] = []
        seen = set()

        def add(identity: Dict[str, str]) -> None:
            key = identity['username'].lower()
            if key not in seen:
                seen.add(key)
                identities.append(identity)

        for alias in member.aliases_for('opsgenie'):
            identity = self._resolve_user(alias) if self._user_lookup_available else None

            if identity:
                add(identity)
            else:
                # Unverified: carry every plausible username form and let the
                # alert search and log matching decide which one is real.
                for candidate in self._username_candidates(alias):
                    add({'id': '', 'username': candidate})

        return identities

    def _username_candidates(self, alias: str) -> List[str]:
        """Username forms to try for a roster alias.

        OpsGenie logins are usually email addresses, so a bare alias is tried
        as-is and then qualified with the configured domain.
        """
        if '@' in alias:
            return [alias]
        return [alias, f"{alias}@{self.email_domain}"]

    # ------------------------------------------------------------------
    # Phase 1: Data fetching
    # ------------------------------------------------------------------

    def _resolve_user(self, alias: str) -> Optional[Dict[str, str]]:
        """Look up an OpsGenie user to confirm a roster alias, if permitted.

        Returns None when the user does not exist *or* when the API key cannot
        read users at all. The caller treats both as "unverified" and falls back
        to the candidate usernames, so a None here degrades precision rather
        than dropping the person.
        """
        if alias in self._user_cache:
            return self._user_cache[alias]

        for candidate in self._username_candidates(alias):
            url = f"{self.base_url}/v2/users/{candidate}"
            try:
                response = self.session.get(url, headers=self.headers, timeout=15)
                if response.status_code == 200:
                    data = response.json().get('data', {})
                    identity = {
                        'id': str(data.get('id', '')),
                        'username': data.get('username', candidate),
                    }
                    self._user_cache[alias] = identity
                    return identity
                if response.status_code in (401, 403):
                    # Alert reads already passed the preflight, so this is a
                    # narrower permission gap. Stop asking and match on usernames.
                    self._user_lookup_available = False
                    logger.info(
                        f"OpsGenie user lookup is not permitted for this API key "
                        f"({response.status_code}); matching alert activity by "
                        f"username instead. Attribution still works — add explicit "
                        f"'opsgenie' aliases to team.json if anyone comes back empty.")
                    break
                if response.status_code != 404:
                    # 404 is the expected "no such user" answer; anything else is
                    # worth surfacing rather than reading as an unknown alias.
                    logger.warning(f"OpsGenie user lookup for {candidate} returned "
                                   f"{response.status_code}: {response.text[:200]}")
            except Exception as e:
                # Reached only for transport-level failures; a bad URL or key is
                # already caught by verify_connection(), so this is per-user and
                # genuinely unexpected.
                logger.warning(f"OpsGenie user lookup error for {candidate}: {e}")

        self._user_cache[alias] = None
        return None

    def _fetch_alerts(self, identities: List[Dict[str, str]],
                      start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """Fetch alerts this person plausibly touched, within the date window.

        The search covers alerts where the person is the owner *or* a recipient,
        which is a superset of the ones they acted on. Actual attribution is done
        from the logs in phase 2 — this query only narrows what to fetch.
        """
        start_ms = int(start_date.timestamp() * 1000)
        end_ms = int(end_date.timestamp() * 1000)

        alerts: Dict[str, Dict[str, Any]] = {}

        capped = False

        for identity in identities:
            if capped:
                break

            username = identity['username']
            query = (f'(owner:"{username}" OR recipients:"{username}") '
                     f'AND createdAt >= {start_ms} AND createdAt <= {end_ms}')

            offset = 0
            while True:
                params = {
                    'query': query,
                    'limit': SEARCH_PAGE_SIZE,
                    'offset': offset,
                    'sort': 'createdAt',
                    'order': 'desc',
                }
                try:
                    response = self.session.get(
                        f"{self.base_url}/v2/alerts",
                        headers=self.headers, params=params, timeout=20)
                    if response.status_code != 200:
                        logger.warning(
                            f"OpsGenie alert search failed for {username}: "
                            f"{response.status_code} {response.text[:200]}")
                        break
                    page = response.json().get('data', [])
                except Exception as e:
                    logger.debug(f"OpsGenie alert fetch error for {username}: {e}")
                    break

                if not page:
                    break

                for alert in page:
                    if alert.get('id'):
                        alerts[alert['id']] = alert

                if len(alerts) >= MAX_ALERTS_PER_MEMBER:
                    logger.warning(
                        f"OpsGenie alert cap reached for {username} "
                        f"({MAX_ALERTS_PER_MEMBER}); counts below are truncated. "
                        f"Narrow the date range for a complete picture.")
                    capped = True
                    break

                if len(page) < SEARCH_PAGE_SIZE:
                    break
                offset += SEARCH_PAGE_SIZE

        return list(alerts.values())

    def _fetch_alert_logs(self, alert_id: str) -> List[Dict[str, Any]]:
        """Fetch the action log for one alert (cached across members)."""
        if alert_id in self._logs_cache:
            return self._logs_cache[alert_id]

        logs: List[Dict[str, Any]] = []
        params = {'limit': SEARCH_PAGE_SIZE, 'order': 'asc'}
        url = f"{self.base_url}/v2/alerts/{alert_id}/logs"

        while url:
            try:
                response = self.session.get(
                    url, headers=self.headers, params=params, timeout=15)
                if response.status_code != 200:
                    logger.debug(
                        f"OpsGenie log fetch failed for alert {alert_id}: "
                        f"{response.status_code}")
                    break
                body = response.json()
                logs.extend(body.get('data', []))
                # paging.next is a fully-formed URL when more logs remain
                url = body.get('paging', {}).get('next')
                params = None
            except Exception as e:
                logger.debug(f"OpsGenie log fetch error for alert {alert_id}: {e}")
                break

        self._logs_cache[alert_id] = logs
        return logs

    # ------------------------------------------------------------------
    # Phase 2: Analysis (pure computation)
    # ------------------------------------------------------------------

    def _analyze_metrics(self, alerts: List[Dict[str, Any]],
                         alert_logs: Dict[str, List[Dict[str, Any]]],
                         actor_keys: Set[str],
                         member_tz: Optional[Any] = None) -> Dict[str, Any]:
        """Analyze fetched alert data into structured metrics."""
        actions = self._attribute_actions(alerts, alert_logs, actor_keys)

        return {
            'alert_response': self._analyze_alert_response(actions),
            'escalations': self._analyze_escalations(actions),
            'response_times': self._analyze_response_times(alerts, actions),
            'alert_distribution': self._analyze_alert_distribution(alerts, actions),
            'on_call_burden': self._analyze_on_call_burden(alerts, actions, member_tz),
        }

    def _attribute_actions(self, alerts: List[Dict[str, Any]],
                           alert_logs: Dict[str, List[Dict[str, Any]]],
                           actor_keys: Set[str]) -> Dict[str, Any]:
        """Walk every alert log and attribute actions to this member.

        Returns per-action sets of alert ids (sets, so an alert acknowledged
        twice counts once) plus the timestamps needed for response times.
        """
        acknowledged: Set[str] = set()
        closed: Set[str] = set()
        escalated_by: Set[str] = set()
        escalated_to: Set[str] = set()
        commented: Set[str] = set()
        assigned: Set[str] = set()
        involved: Set[str] = set()
        # alert id -> epoch ms of this member's first ack, for response times
        ack_times: Dict[str, int] = {}

        for alert in alerts:
            alert_id = alert.get('id')
            if not alert_id:
                continue

            for entry in alert_logs.get(alert_id, []):
                log_type = (entry.get('type') or '').lower()
                owner = (entry.get('owner') or '').lower()
                text = entry.get('log') or ''
                created_at = entry.get('createdAt')

                mine = owner in actor_keys if owner else False

                # An escalation names its target in the log text rather than in
                # the owner field, so it is the one action attributed by text.
                if self._is_escalation(log_type, text):
                    targets = self._escalation_targets(text)
                    if mine:
                        escalated_by.add(alert_id)
                        involved.add(alert_id)
                    if targets & actor_keys:
                        escalated_to.add(alert_id)
                        involved.add(alert_id)
                    continue

                if not mine:
                    continue

                involved.add(alert_id)

                if log_type == 'acknowledge' or 'acknowledged' in text.lower():
                    acknowledged.add(alert_id)
                    ts = self._epoch_ms(created_at)
                    if ts is not None and (alert_id not in ack_times or ts < ack_times[alert_id]):
                        ack_times[alert_id] = ts
                elif log_type == 'close' or 'closed' in text.lower():
                    closed.add(alert_id)
                elif log_type in ('addnote', 'note') or 'note added' in text.lower():
                    commented.add(alert_id)
                elif log_type == 'assign' or 'assigned' in text.lower():
                    assigned.add(alert_id)

        return {
            'acknowledged': acknowledged,
            'closed': closed,
            'escalated_by': escalated_by,
            'escalated_to': escalated_to,
            'commented': commented,
            'assigned': assigned,
            'involved': involved,
            'ack_times': ack_times,
        }

    @staticmethod
    def _is_escalation(log_type: str, text: str) -> bool:
        """True if a log entry represents an escalation."""
        return 'escalat' in log_type or 'escalat' in text.lower()

    @staticmethod
    def _escalation_targets(text: str) -> Set[str]:
        """Extract the usernames/emails an escalation was routed to.

        OpsGenie escalation logs read like
        ``Escalated to [jsmith@example.com]`` or
        ``Escalation [Tier2] escalated alert to [oncall-team]``, so bracketed
        tokens are the reliable signal.
        """
        return {token.strip().lower()
                for token in re.findall(r'\[([^\]]+)\]', text)
                if token.strip()}

    @staticmethod
    def _epoch_ms(value: Any) -> Optional[int]:
        """Coerce an OpsGenie timestamp into epoch milliseconds."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except (ValueError, TypeError):
            return None

    def _analyze_alert_response(self, actions: Dict[str, Any]) -> Dict[str, Any]:
        """Headline counts: what this person did with alerts."""
        return {
            'acknowledged': len(actions['acknowledged']),
            'closed': len(actions['closed']),
            'notes_added': len(actions['commented']),
            'assigned': len(actions['assigned']),
            'alerts_involved': len(actions['involved']),
        }

    def _analyze_escalations(self, actions: Dict[str, Any]) -> Dict[str, Any]:
        """Escalations in both directions.

        ``escalated_by`` is about handoff behaviour; ``escalated_to`` is about
        alerts that landed here because an earlier tier did not respond.
        """
        return {
            'escalated_by': len(actions['escalated_by']),
            'escalated_to': len(actions['escalated_to']),
        }

    def _analyze_response_times(self, alerts: List[Dict[str, Any]],
                                actions: Dict[str, Any]) -> Dict[str, Any]:
        """Time from alert creation to this member's acknowledgement."""
        ack_minutes = []
        created_lookup = {a['id']: self._epoch_ms(a.get('createdAt'))
                          for a in alerts if a.get('id')}

        for alert_id, ack_ms in actions['ack_times'].items():
            created_ms = created_lookup.get(alert_id)
            if created_ms is None or ack_ms < created_ms:
                continue
            ack_minutes.append((ack_ms - created_ms) / 60000.0)

        ack_minutes.sort()
        return {
            'avg_ack_minutes': round(sum(ack_minutes) / len(ack_minutes), 2) if ack_minutes else 0,
            'median_ack_minutes': round(self._median(ack_minutes), 2) if ack_minutes else 0,
            'slowest_ack_minutes': round(ack_minutes[-1], 2) if ack_minutes else 0,
            'acks_measured': len(ack_minutes),
        }

    @staticmethod
    def _median(sorted_values: List[float]) -> float:
        """Median of an already-sorted list (empty list returns 0)."""
        n = len(sorted_values)
        if n == 0:
            return 0.0
        mid = n // 2
        if n % 2:
            return sorted_values[mid]
        return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0

    def _analyze_alert_distribution(self, alerts: List[Dict[str, Any]],
                                    actions: Dict[str, Any]) -> Dict[str, Any]:
        """Priority mix of the alerts this member actually acted on."""
        priority_dist: Dict[str, int] = {}
        involved = actions['involved']

        for alert in alerts:
            if alert.get('id') not in involved:
                continue
            priority = alert.get('priority') or 'unknown'
            priority_dist[priority] = priority_dist.get(priority, 0) + 1

        return {'priority_distribution': priority_dist}

    def _analyze_on_call_burden(self, alerts: List[Dict[str, Any]],
                               actions: Dict[str, Any],
                               member_tz: Optional[Any] = None) -> Dict[str, Any]:
        """When the pager went off — the obstacle-spotting metric.

        Buckets are computed in *member_tz*, this person's local time, because
        "after hours" has no shared meaning across a distributed team: 21:00 for
        one person is 19:00 for another. Weekend takes precedence over
        after-hours so the two counts never overlap.

        This is a burden signal, not a performance signal.
        """
        after_hours = 0
        weekend = 0
        involved = actions['involved']

        for alert in alerts:
            if alert.get('id') not in involved:
                continue
            created_ms = self._epoch_ms(alert.get('createdAt'))
            if created_ms is None:
                continue
            bucket = classify_work_time(created_ms, member_tz, self.business_hours)
            if bucket == 'weekend':
                weekend += 1
            elif bucket == 'after_hours':
                after_hours += 1

        return {
            'after_hours_alerts': after_hours,
            'weekend_alerts': weekend,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_metrics() -> Dict[str, Any]:
        """Return an empty metrics structure for users not found in OpsGenie."""
        return {
            'alert_response': {
                'acknowledged': 0, 'closed': 0, 'notes_added': 0,
                'assigned': 0, 'alerts_involved': 0,
            },
            'escalations': {
                'escalated_by': 0, 'escalated_to': 0,
            },
            'response_times': {
                'avg_ack_minutes': 0, 'median_ack_minutes': 0,
                'slowest_ack_minutes': 0, 'acks_measured': 0,
            },
            'alert_distribution': {
                'priority_distribution': {},
            },
            'on_call_burden': {
                'after_hours_alerts': 0, 'weekend_alerts': 0,
            },
        }
