#!/usr/bin/env python3
"""
Grafana IRM Metrics Collector

The Grafana IRM counterpart to :mod:`opsgenie_collector` — it answers the same
question ("who actually responded to our alerts?") against the tool the team is
migrating *to*.  Both collectors run side by side during the migration and their
per-member numbers are rolled up into one alert view by
``utils.merge_alert_metrics``.

Two differences from OpsGenie are structural rather than incidental, and both
shape the code below:

**Attribution comes from the alert group record, not from an action log.**
OpsGenie exposes ``/v2/alerts/{id}/logs``, which names the actor behind every
action.  Grafana IRM's public API has no equivalent, so the only attributable
actions are the two the alert group itself records: ``acknowledged_by`` and
``resolved_by``.  Notes, assignment, and escalation therefore have no IRM
source, and the metrics dict simply **omits** those keys rather than reporting
zero for them — a zero here would read as "nobody escalated anything", which is
not what the data says.  The merged view takes those counts from OpsGenie for as
long as OpsGenie is still in use.

**User lookup is mandatory, not enrichment.** ``acknowledged_by`` is an opaque
OnCall user id (``U4DNY931HHJS5``), so a roster alias cannot be matched against
an alert group without first resolving ids through ``/api/v1/users/``.  Where
the OpsGenie collector degrades to username matching when it cannot read users,
this one has nothing to degrade to: it fails loudly and names the missing
permission instead of returning a page of zeros.

API reference: https://grafana.com/docs/oncall/latest/oncall-api-reference/
"""

from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Set, Tuple

from utils import (logger, create_session, classify_work_time, resolve_timezone,
                   parse_business_hours, summarize_ack_minutes)
from team import TeamMember

# Page size for every list endpoint. The API caps `perpage` at 100 and serves
# anything larger at the maximum rather than erroring.
PAGE_SIZE = 100

# Ceiling on alert groups fetched per run. Unlike OpsGenie — which has to search
# once per member because its query language takes a username — the IRM API has
# no per-user filter, so alert groups are fetched *once for the whole team* and
# attributed locally. That makes this a team-wide cap, not a per-member one.
# Exceeding it is logged, never silent: a truncated count that looks complete is
# worse than a loud warning.
MAX_ALERT_GROUPS = 5000

# Ceiling on the user directory walk. Generous next to any real org, and only a
# guard against paging forever.
MAX_USERS = 5000

# Guard against a paging loop if the API ever returns a `next` link that does not
# advance. 100 pages is well past MAX_ALERT_GROUPS at PAGE_SIZE.
MAX_PAGES = 100

# Alert group label key read as the severity/priority of an alert, mirroring
# OpsGenie's P1-P5. Overridable because IRM labels are free-form.
DEFAULT_SEVERITY_LABEL = 'severity'

# Payload locations checked for a severity when the alert group carries no
# severity label. Grafana Alerting puts rule labels here.
_PAYLOAD_SEVERITY_PATHS = (
    ('labels', 'severity'),
    ('commonLabels', 'severity'),
    ('severity',),
)


class GrafanaIRMCollector:
    def __init__(self, config: Dict[str, Any]):
        self.token = config['token']
        self.base_url = self._normalize_base_url(config.get('api_url') or '')
        # Set only for a Grafana service account token, which is authenticated
        # differently from a legacy OnCall API key (see _auth_variants).
        self.stack_url = (config.get('stack_url') or '').strip().rstrip('/')
        self.email_domain = config.get('email_domain', '')
        # Fallback timezone for members whose roster entry does not name one and
        # whose IRM user record has no timezone either.
        self.default_timezone = config.get('default_timezone') or 'UTC'
        # Working-day window applied in each member's local time.
        self.business_hours = parse_business_hours(config.get('business_hours'))
        self.severity_label = config.get('severity_label') or DEFAULT_SEVERITY_LABEL
        self.session = create_session()

        # Chosen by the preflight and reused for every later request, so the
        # fallback probe happens once rather than on every call.
        self._headers: Optional[Dict[str, str]] = None

        # Lazily built by _user_index(): lookup key -> OnCall user id, plus the
        # id -> user record map used for timezone fallback.
        self._by_key: Optional[Dict[str, str]] = None
        self._by_id: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Connection and authentication
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_base_url(raw: str) -> str:
        """Coerce a configured URL into the OnCall API base URL.

        The value wanted here is the *OnCall API endpoint* from
        IRM -> Settings -> Admin & API (on Grafana Cloud it looks like
        ``https://oncall-prod-us-central-0.grafana.net/oncall``), which is not
        the stack URL people naturally copy out of the browser. A bare stack host
        serves the Grafana UI, not this API, so it is rejected in
        ``validate_config`` with that instruction rather than turned into a run
        where every request 404s.

        A pasted ``/api/v1`` suffix is trimmed, since every request below appends
        its own.
        """
        url = raw.strip().rstrip('/')
        if not url:
            return url

        if '://' not in url:
            url = f"https://{url}"
            logger.warning(f"Grafana IRM API URL had no scheme; assuming {url}")

        for suffix in ('/api/v1', '/api'):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                logger.info(f"Trimmed {suffix!r} from GRAFANA_IRM_API_URL; requests "
                            f"append their own path. Using {url}")
                break

        return url.rstrip('/')

    def _auth_variants(self) -> List[Tuple[str, Dict[str, str]]]:
        """Header sets to try, most likely first.

        Grafana IRM accepts two credentials that are *not* interchangeable:

        * a Grafana **service account token** (``glsa_...``), sent as
          ``Authorization: Bearer <token>`` and requiring an ``X-Grafana-URL``
          header naming the stack; and
        * a legacy **OnCall API key**, sent bare as ``Authorization: <token>``
          with no stack header.

        Sending one in the other's form is a 401 that looks exactly like a bad
        token, and it is the single most common way this integration is
        misconfigured. So both are tried at preflight and the working one is
        logged by name, rather than leaving the reader to guess which of the two
        they got wrong.
        """
        bearer = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        }
        if self.stack_url:
            bearer['X-Grafana-URL'] = self.stack_url

        legacy = {
            'Authorization': self.token,
            'Content-Type': 'application/json',
        }

        # A stack URL is meaningless to a legacy key, so its presence is the best
        # available signal for which credential the operator configured.
        if self.stack_url or self.token.startswith('glsa_'):
            return [('service account token', bearer), ('legacy OnCall API key', legacy)]
        return [('legacy OnCall API key', legacy), ('service account token', bearer)]

    def verify_connection(self) -> bool:
        """Check reachability and alert-group read access before per-member work.

        Probes ``/api/v1/alert_groups/`` — the one capability this collector
        cannot work without — rather than a general-purpose endpoint, so a token
        scoped to exactly what is needed passes.
        """
        url = f"{self.base_url}/api/v1/alert_groups/"
        last_status = None
        last_body = ''

        for label, headers in self._auth_variants():
            try:
                response = self.session.get(url, headers=headers,
                                            params={'perpage': 1}, timeout=15)
            except Exception as e:
                logger.error(f"Grafana IRM is unreachable at {self.base_url}: {e}")
                return False

            if response.status_code == 200:
                self._headers = headers
                logger.info(f"Grafana IRM connection verified using a {label} "
                            f"(alert group read access confirmed)")
                return True

            last_status = response.status_code
            last_body = response.text[:200]

            if response.status_code not in (401, 403):
                # Only an auth failure is worth re-probing with the other
                # credential form; a 404 or 500 means the same thing either way.
                break

        if last_status in (401, 403):
            logger.error(
                f"Grafana IRM rejected both supported credential forms "
                f"({last_status}). For a Grafana service account token, set "
                f"GRAFANA_IRM_STACK_URL to your stack (e.g. https://myorg.grafana.net) "
                f"and give the service account the 'grafana-oncall-app.alert-groups:read' "
                f"and 'grafana-oncall-app.user-settings:read' permissions. For a legacy "
                f"OnCall API key, leave GRAFANA_IRM_STACK_URL unset.")
        elif last_status == 404:
            logger.error(
                f"Grafana IRM returned 404 for {url}. GRAFANA_IRM_API_URL must be the "
                f"OnCall API endpoint from IRM -> Settings -> Admin & API (e.g. "
                f"https://oncall-prod-us-central-0.grafana.net/oncall), not your "
                f"Grafana stack URL.")
        else:
            logger.error(f"Grafana IRM preflight failed at {url}: {last_status} {last_body}")
        return False

    # ------------------------------------------------------------------
    # Collection entry point
    # ------------------------------------------------------------------

    def collect_team_metrics(self, members: List[TeamMember],
                             start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Collect Grafana IRM metrics using the two-phase pattern.

        Phase 1: Fetch the user directory and the window's alert groups.
        Phase 2: Analyze the fetched data into per-member metrics.

        Both fetches are done once for the whole team — the alert groups endpoint
        takes no per-user filter — and attribution happens locally in phase 2.
        """
        empty = {member.id: self._empty_metrics() for member in members}

        # Fail once, clearly, rather than once per member.
        if not self.verify_connection():
            logger.error("Skipping Grafana IRM collection; see the error above.")
            return empty

        # Phase 1a: the user directory, without which no alert group can be
        # attributed to anyone. This is fatal here, unlike in OpsGenie.
        by_key = self._user_index()
        if by_key is None:
            logger.error("Skipping Grafana IRM collection; see the error above.")
            return empty

        # Phase 1b: every alert group created in the window, once.
        alert_groups = self._fetch_alert_groups(start_date, end_date)
        logger.info(f"Grafana IRM returned {len(alert_groups)} alert group(s) in the window")

        # Phase 2: attribute and analyze, per member.
        team_metrics = {}
        total_touched = 0
        unmatched = []

        for member in members:
            logger.info(f"Analyzing Grafana IRM metrics for {member.id}")

            user_ids = self._user_ids_for(member, by_key)
            if not user_ids:
                unmatched.append(member.id)

            # Out-of-hours buckets use this person's own timezone, so a
            # distributed team is measured against local nights and weekends
            # rather than one shared window. IRM knows each user's timezone, so
            # it can fill in for a roster entry that does not name one.
            member_tz = resolve_timezone(
                member.timezone or self._irm_timezone(user_ids) or self.default_timezone)

            metrics = self._analyze_metrics(alert_groups, user_ids, member_tz)
            total_touched += metrics['alert_response']['alerts_involved']
            team_metrics[member.id] = metrics

        if unmatched:
            logger.warning(
                f"No Grafana IRM user matched these roster members: {', '.join(unmatched)}. "
                f"Each alias is tried as a username, as an email, and as "
                f"<alias>@{self.email_domain or '<GRAFANA_IRM_EMAIL_DOMAIN>'}; add an "
                f"explicit \"grafana_irm\" alias in team.json for anyone whose IRM login "
                f"is none of those.")

        if alert_groups and total_touched == 0:
            logger.warning(
                "Grafana IRM returned alert groups but none were acknowledged or "
                "resolved by anyone on the roster. If the team was on call during this "
                "window, the likely causes are: the token is scoped to a team that does "
                "not own these alerts, or the roster aliases do not match IRM logins.")

        return team_metrics

    # ------------------------------------------------------------------
    # Phase 1: Data fetching
    # ------------------------------------------------------------------

    def _get_page(self, path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """GET one page of a list endpoint, returning the decoded body or None."""
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, headers=self._headers,
                                        params=params, timeout=30)
        except Exception as e:
            logger.warning(f"Grafana IRM request to {path} failed: {e}")
            return None

        if response.status_code == 429:
            # 300 requests per 5 minutes per key. Worth naming, because the
            # symptom (a short page count) otherwise looks like "no data".
            logger.warning(
                f"Grafana IRM rate-limited the request to {path} (429). Counts below "
                f"are truncated; the limit is 300 requests per 5 minutes per token.")
            return None

        if response.status_code != 200:
            logger.warning(f"Grafana IRM request to {path} returned "
                           f"{response.status_code}: {response.text[:200]}")
            return None

        try:
            return response.json()
        except ValueError as e:
            logger.warning(f"Grafana IRM returned a non-JSON body for {path}: {e}")
            return None

    def _paginate(self, path: str, params: Dict[str, Any],
                  limit: int) -> Tuple[List[Dict[str, Any]], bool]:
        """Walk a paginated list endpoint. Returns (results, hit_limit)."""
        results: List[Dict[str, Any]] = []
        page = 1

        while page <= MAX_PAGES:
            body = self._get_page(path, {**params, 'page': page, 'perpage': PAGE_SIZE})
            if body is None:
                break

            batch = body.get('results') or []
            results.extend(batch)

            if len(results) >= limit:
                return results[:limit], True
            # `next` is null on the last page; a short page means the same thing
            # on deployments that do not send it.
            if not body.get('next') or len(batch) < PAGE_SIZE:
                break
            page += 1

        return results, False

    def _user_index(self) -> Optional[Dict[str, str]]:
        """Build a lookup of every IRM identifier -> OnCall user id.

        Returns None when the directory cannot be read at all, which is fatal:
        alert groups name their actors by opaque id, so without this map nothing
        can be attributed to anyone.

        One list call (paginated) rather than a lookup per alias — the endpoint's
        only filter is an exact ``username`` match, which would miss anyone whose
        roster alias is their email, and the team is far smaller than the rate
        limit either way.
        """
        if self._by_key is not None:
            return self._by_key

        users, _ = self._paginate('/api/v1/users/', {}, limit=MAX_USERS)
        if not users:
            logger.error(
                "Grafana IRM returned no readable users, so no alert group can be "
                "attributed to anyone. The token needs "
                "'grafana-oncall-app.user-settings:read' in addition to alert group "
                "read access.")
            return None

        by_key: Dict[str, str] = {}
        # Email local parts are indexed only when unambiguous — two people at
        # different domains sharing a local part would otherwise silently credit
        # one of them with the other's alerts.
        local_parts: Dict[str, Set[str]] = {}

        for user in users:
            user_id = str(user.get('id') or '')
            if not user_id:
                continue
            self._by_id[user_id] = user

            for value in (user.get('username'), user.get('email')):
                if value:
                    by_key[str(value).strip().lower()] = user_id

            email = (user.get('email') or '').strip().lower()
            if '@' in email:
                local_parts.setdefault(email.split('@', 1)[0], set()).add(user_id)

        for local, ids in local_parts.items():
            if len(ids) == 1 and local not in by_key:
                by_key[local] = next(iter(ids))

        logger.info(f"Grafana IRM user directory resolved ({len(self._by_id)} users)")
        self._by_key = by_key
        return by_key

    def _fetch_alert_groups(self, start_date: datetime,
                            end_date: datetime) -> List[Dict[str, Any]]:
        """Fetch every alert group created in the window, for the whole team.

        The API's ``started_at`` filter takes a ``{start}_{end}`` pair of naive
        second-precision ISO 8601 timestamps interpreted as UTC, so the bounds
        are converted before formatting rather than passed through with an
        offset the API would reject.
        """
        started_at = f"{self._api_timestamp(start_date)}_{self._api_timestamp(end_date)}"

        groups, hit_limit = self._paginate(
            '/api/v1/alert_groups/', {'started_at': started_at}, limit=MAX_ALERT_GROUPS)

        if hit_limit:
            logger.warning(
                f"Grafana IRM alert group cap reached ({MAX_ALERT_GROUPS}); counts below "
                f"are truncated. Narrow the date range for a complete picture.")

        return groups

    @staticmethod
    def _api_timestamp(value: datetime) -> str:
        """Format a datetime the way the ``started_at`` filter expects (UTC, naive)."""
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime('%Y-%m-%dT%H:%M:%S')

    # ------------------------------------------------------------------
    # Identity matching
    # ------------------------------------------------------------------

    def _user_ids_for(self, member: TeamMember, by_key: Dict[str, str]) -> Set[str]:
        """Resolve one roster member to their OnCall user id(s).

        A person may hold more than one IRM identity (a migrated account and a
        current one, say), so every alias that resolves is kept and their actions
        are combined under the member's canonical id.
        """
        user_ids: Set[str] = set()
        for alias in member.aliases_for('grafana_irm'):
            for candidate in self._alias_candidates(alias):
                user_id = by_key.get(candidate)
                if user_id:
                    user_ids.add(user_id)
        return user_ids

    def _alias_candidates(self, alias: str) -> List[str]:
        """Identifier forms to try for a roster alias.

        IRM logins may be a bare username or an email depending on how the stack
        authenticates, so a bare alias is tried as-is and then qualified with the
        configured domain.
        """
        alias = alias.strip().lower()
        if not alias:
            return []
        if '@' in alias or not self.email_domain:
            return [alias]
        return [alias, f"{alias}@{self.email_domain.lower()}"]

    def _irm_timezone(self, user_ids: Set[str]) -> Optional[str]:
        """The timezone IRM holds for this person, if it knows one.

        Used only when the roster does not name a timezone. IRM asks each user
        for theirs, which makes it a better default than the team-wide fallback
        for anyone who set it.
        """
        for user_id in sorted(user_ids):
            tz = (self._by_id.get(user_id, {}).get('timezone') or '').strip()
            if tz:
                return tz
        return None

    # ------------------------------------------------------------------
    # Phase 2: Analysis (pure computation)
    # ------------------------------------------------------------------

    def _analyze_metrics(self, alert_groups: List[Dict[str, Any]],
                         user_ids: Set[str],
                         member_tz: Optional[Any] = None) -> Dict[str, Any]:
        """Analyze fetched alert group data into structured metrics.

        There is deliberately no ``escalations`` block and no notes/assigned
        counts: the IRM public API records neither, and reporting them as zero
        would misrepresent an unmeasured thing as a measured absence.
        """
        actions = self._attribute_actions(alert_groups, user_ids)

        return {
            'alert_response': self._analyze_alert_response(actions),
            'response_times': self._analyze_response_times(actions),
            'alert_distribution': self._analyze_alert_distribution(alert_groups, actions),
            'on_call_burden': self._analyze_on_call_burden(alert_groups, actions, member_tz),
        }

    def _attribute_actions(self, alert_groups: List[Dict[str, Any]],
                           user_ids: Set[str]) -> Dict[str, Any]:
        """Credit each alert group's recorded actions to this member.

        ``acknowledged_by`` and ``resolved_by`` are independent fields, which is
        what makes the common split case work: one person acknowledges, another
        resolves, and each is credited for their own action rather than both
        landing on whoever touched it last.
        """
        acknowledged: Set[str] = set()
        closed: Set[str] = set()
        involved: Set[str] = set()
        # group id -> minutes from creation to this member's acknowledgement
        ack_minutes: Dict[str, float] = {}

        for group in alert_groups:
            group_id = group.get('id')
            if not group_id:
                continue

            if str(group.get('acknowledged_by') or '') in user_ids:
                acknowledged.add(group_id)
                involved.add(group_id)
                elapsed = self._elapsed_minutes(group.get('created_at'),
                                                group.get('acknowledged_at'))
                if elapsed is not None:
                    ack_minutes[group_id] = elapsed

            if str(group.get('resolved_by') or '') in user_ids:
                closed.add(group_id)
                involved.add(group_id)

        return {
            'acknowledged': acknowledged,
            'closed': closed,
            'involved': involved,
            'ack_minutes': ack_minutes,
        }

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime]:
        """Parse an IRM ISO 8601 timestamp into an aware datetime."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @classmethod
    def _elapsed_minutes(cls, start: Any, end: Any) -> Optional[float]:
        """Minutes between two IRM timestamps, or None if either is unusable.

        A negative interval is dropped rather than returned: it can only mean
        clock skew or a record written out of order, and a negative
        time-to-acknowledge would drag the average somewhere impossible.
        """
        start_dt = cls._parse_timestamp(start)
        end_dt = cls._parse_timestamp(end)
        if start_dt is None or end_dt is None:
            return None
        elapsed = (end_dt - start_dt).total_seconds() / 60.0
        return elapsed if elapsed >= 0 else None

    def _analyze_alert_response(self, actions: Dict[str, Any]) -> Dict[str, Any]:
        """Headline counts: what this person did with alert groups.

        ``closed`` rather than ``resolved`` so the key matches the OpsGenie
        collector's and the two merge without translation; IRM calls the same
        action "resolve".
        """
        return {
            'acknowledged': len(actions['acknowledged']),
            'closed': len(actions['closed']),
            'alerts_involved': len(actions['involved']),
        }

    def _analyze_response_times(self, actions: Dict[str, Any]) -> Dict[str, Any]:
        """Time from alert group creation to this member's acknowledgement.

        The per-alert samples are carried through in the summary alongside the
        averages, so that ``merge_alert_metrics`` can combine this block with the
        OpsGenie one exactly during the migration.
        """
        return summarize_ack_minutes(list(actions['ack_minutes'].values()))

    def _analyze_alert_distribution(self, alert_groups: List[Dict[str, Any]],
                                    actions: Dict[str, Any]) -> Dict[str, Any]:
        """Severity mix of the alert groups this member actually acted on.

        Kept under ``priority_distribution`` so it merges with the OpsGenie block
        of the same name without a translation layer. The *values* are IRM's own
        vocabulary, though: OpsGenie has a fixed P1-P5 scale, while IRM severity
        is a free-form label, so the two sources contribute different keys to the
        merged distribution rather than one being mapped onto the other.
        """
        distribution: Dict[str, int] = {}
        involved = actions['involved']

        for group in alert_groups:
            if group.get('id') not in involved:
                continue
            severity = self._severity_for(group)
            distribution[severity] = distribution.get(severity, 0) + 1

        return {'priority_distribution': distribution}

    def _severity_for(self, group: Dict[str, Any]) -> str:
        """Best available severity for an alert group.

        Prefers an alert group label, which is what a team that has set labels up
        will expect to be read. Falls back to the triggering payload, where
        Grafana Alerting puts the rule's own labels.
        """
        wanted = self.severity_label.lower()

        for label in group.get('labels') or []:
            if not isinstance(label, dict):
                continue
            # The public API nests each half as {"id": ..., "name": ...}, but a
            # flat {"key": "severity", "value": "critical"} shows up on some
            # OnCall versions. Read either rather than raising on the one that
            # happens to be deployed.
            key = self._label_part(label.get('key'))
            if key and key.lower() == wanted:
                value = self._label_part(label.get('value'))
                if value:
                    return value.lower()

        payload = (group.get('last_alert') or {}).get('payload') or {}
        if isinstance(payload, dict):
            for path in _PAYLOAD_SEVERITY_PATHS:
                value: Any = payload
                for step in path:
                    value = value.get(step) if isinstance(value, dict) else None
                    if value is None:
                        break
                if isinstance(value, str) and value.strip():
                    return value.strip().lower()

        return 'unknown'

    @staticmethod
    def _label_part(part: Any) -> str:
        """Read one half of an alert group label, nested or flat."""
        if isinstance(part, dict):
            part = part.get('name') or part.get('id') or ''
        return str(part).strip() if part else ''

    def _analyze_on_call_burden(self, alert_groups: List[Dict[str, Any]],
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

        for group in alert_groups:
            if group.get('id') not in involved:
                continue
            created = self._parse_timestamp(group.get('created_at'))
            if created is None:
                continue
            created_ms = int(created.timestamp() * 1000)
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
        """Return an empty metrics structure for users with no IRM activity.

        Mirrors ``_analyze_metrics``: no ``escalations`` block and no
        notes/assigned counts, because IRM does not measure them.
        """
        return {
            'alert_response': {
                'acknowledged': 0, 'closed': 0, 'alerts_involved': 0,
            },
            'response_times': summarize_ack_minutes([]),
            'alert_distribution': {
                'priority_distribution': {},
            },
            'on_call_burden': {
                'after_hours_alerts': 0, 'weekend_alerts': 0,
            },
        }
