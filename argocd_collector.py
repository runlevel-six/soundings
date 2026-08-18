#!/usr/bin/env python3
"""
ArgoCD Metrics Collector

Collects sync activity from both ``status.history`` (limited rolling
window kept by the ArgoCD controller) **and** the Kubernetes events
stream (``/api/v1/applications/{name}/events``).  The events stream
captures failed syncs and recent activity that ``status.history`` may
have already evicted.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
from utils import logger, parse_datetime_with_timezone, safe_api_request, create_session
from team import TeamMember


class ArgoCDCollector:
    def __init__(self, config):
        self.instances = config.get('instances', [])
        self.session = create_session()

    def collect_team_metrics(self, members: List[TeamMember],
                             start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Collect ArgoCD deployment and sync metrics across all instances.

        Fetches all instance data once up front, then analyzes per-member from
        cache. A member may map to several ArgoCD aliases; syncs initiated under
        any of them are attributed to that member.
        """
        # ---- Phase 1: fetch data once per instance ----
        instance_cache = {}
        for instance in self.instances:
            instance_cache[instance['name']] = self._fetch_instance_data(instance)

        # ---- Phase 2: analyze per member from cached data ----
        team_metrics = {}
        for member in members:
            logger.info(f"Analyzing ArgoCD metrics for {member.id} across {len(self.instances)} instances")
            aliases = member.aliases_for('argocd')
            sync_activity = self._analyze_sync_activity(aliases, instance_cache, start_date, end_date)
            team_metrics[member.id] = {
                'sync_activity': sync_activity,
                'application_health': self._analyze_application_health(instance_cache),
                'deployment_patterns': self._analyze_deployment_patterns(
                    sync_activity, start_date, end_date),
                'instances_summary': self._analyze_instances_summary(instance_cache),
            }

        return team_metrics

    # ------------------------------------------------------------------
    # Data fetching (called once per instance)
    # ------------------------------------------------------------------

    def _fetch_instance_data(self, instance: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch all apps, their details, and events for one ArgoCD instance."""
        base_url = instance['base_url']
        token = instance['token']
        headers = {'Authorization': f'Bearer {token}'}

        result = {
            'apps_list': [],
            'app_details': {},
            'app_events': {},
            'projects': [],
        }

        try:
            apps_data = safe_api_request(
                f"{base_url}/api/v1/applications", headers=headers, session=self.session)
            if not apps_data or not isinstance(apps_data, dict):
                return result

            apps = apps_data.get('items', [])
            result['apps_list'] = apps

            # Derive unique projects from app list
            projects = set()
            for app in apps:
                project = app.get('spec', {}).get('project')
                if project:
                    projects.add(project)
            result['projects'] = list(projects) or ['default']

            # Fetch full detail and events for each app
            for app in apps:
                app_name = app.get('metadata', {}).get('name')
                if not app_name:
                    continue
                detail = safe_api_request(
                    f"{base_url}/api/v1/applications/{app_name}",
                    headers=headers, session=self.session)
                if detail:
                    result['app_details'][app_name] = detail

                events_data = safe_api_request(
                    f"{base_url}/api/v1/applications/{app_name}/events",
                    headers=headers, session=self.session)
                if events_data and isinstance(events_data, dict):
                    result['app_events'][app_name] = events_data.get('items', [])

        except Exception as e:
            logger.error(f"Error fetching data from {instance['name']}: {e}")

        return result

    # ------------------------------------------------------------------
    # Analysis methods (pure computation over cached data)
    # ------------------------------------------------------------------

    def _analyze_sync_activity(self, aliases: List[str],
                               instance_cache: Dict[str, Dict],
                               start_date: datetime,
                               end_date: datetime) -> Dict[str, Any]:
        """Analyze sync activity from cached instance data.

        Merges sync records from both ``status.history`` and the events
        stream so that failed syncs and recently-evicted history entries
        are not lost.  Duplicates (same app, same minute) are removed,
        preferring the richer ``history`` record.
        """
        sync_metrics = {
            'total_syncs': 0,
            'successful_syncs': 0,
            'failed_syncs': 0,
            'manual_syncs': 0,
            'auto_syncs': 0,
            'by_instance': {}
        }

        for instance in self.instances:
            instance_name = instance['name']
            cache = instance_cache.get(instance_name, {})
            instance_syncs = {'syncs': 0, 'successes': 0, 'failures': 0}

            # -- Build normalised sync records from both sources ----------
            all_syncs: List[Dict[str, Any]] = []

            # Source 1: status.history
            for app_name, app_detail in cache.get('app_details', {}).items():
                history = app_detail.get('status', {}).get('history', [])
                for entry in history:
                    sync_time = self._parse_sync_time(entry)
                    initiated_by = entry.get('initiatedBy', {})
                    all_syncs.append({
                        'app': app_name,
                        'time': sync_time,
                        'username': initiated_by.get('username', ''),
                        'automated': initiated_by.get('automated', False),
                        'success': self._determine_sync_success(entry),
                        'source': 'history',
                    })

            # Source 2: application events
            for app_name, events in cache.get('app_events', {}).items():
                if events:
                    all_syncs.extend(self._parse_sync_events(events, app_name))

            # -- Deduplicate (history wins over events) -------------------
            all_syncs = self._deduplicate_syncs(all_syncs)

            # -- Filter by date-range and user, then tally ----------------
            for sync in all_syncs:
                try:
                    sync_time = sync.get('time')
                    if sync_time and (sync_time < start_date or sync_time >= end_date):
                        continue

                    if not self._user_matches_aliases(sync.get('username', ''), aliases):
                        continue

                    sync_metrics['total_syncs'] += 1
                    instance_syncs['syncs'] += 1

                    if sync['automated']:
                        sync_metrics['auto_syncs'] += 1
                    else:
                        sync_metrics['manual_syncs'] += 1

                    if sync['success']:
                        sync_metrics['successful_syncs'] += 1
                        instance_syncs['successes'] += 1
                    else:
                        sync_metrics['failed_syncs'] += 1
                        instance_syncs['failures'] += 1

                except Exception as sync_error:
                    logger.debug(f"Error processing sync record: {sync_error}")

            sync_metrics['by_instance'][instance_name] = instance_syncs

        return sync_metrics

    def _analyze_application_health(self, instance_cache: Dict[str, Dict]) -> Dict[str, Any]:
        """Analyze application health from cached data (instance-wide, not per-user)."""
        health_metrics = {
            'healthy_apps': 0,
            'unhealthy_apps': 0,
            'degraded_apps': 0,
            'unknown_apps': 0,
            'by_instance': {}
        }

        for instance in self.instances:
            instance_name = instance['name']
            cache = instance_cache.get(instance_name, {})

            instance_health = {'healthy': 0, 'unhealthy': 0, 'degraded': 0, 'unknown': 0}

            for app in cache.get('apps_list', []):
                try:
                    status = app.get('status', {})
                    health = status.get('health', {}) if status else {}
                    health_status = health.get('status', 'Unknown') if health else 'Unknown'

                    if health_status == 'Healthy':
                        instance_health['healthy'] += 1
                        health_metrics['healthy_apps'] += 1
                    elif health_status in ('Degraded', 'Progressing'):
                        instance_health['degraded'] += 1
                        health_metrics['degraded_apps'] += 1
                    elif health_status in ('Missing', 'Suspended', 'Unknown'):
                        instance_health['unhealthy'] += 1
                        health_metrics['unhealthy_apps'] += 1
                    else:
                        instance_health['unknown'] += 1
                        health_metrics['unknown_apps'] += 1
                except Exception:
                    instance_health['unknown'] += 1
                    health_metrics['unknown_apps'] += 1

            health_metrics['by_instance'][instance_name] = instance_health

        return health_metrics

    def _analyze_deployment_patterns(self, sync_activity: Dict[str, Any],
                                     start_date: datetime,
                                     end_date: datetime) -> Dict[str, Any]:
        """Derive deployment patterns from the already-computed sync_activity."""
        patterns = {
            'deployment_frequency': 'unknown',
            'deployment_stability': 0,
            'rollback_frequency': 0,
            'primary_instance': None
        }

        total_days = (end_date - start_date).days
        if total_days > 0:
            syncs_per_week = (sync_activity['total_syncs'] / total_days) * 7
            if syncs_per_week >= 10:
                patterns['deployment_frequency'] = 'very_high'
            elif syncs_per_week >= 5:
                patterns['deployment_frequency'] = 'high'
            elif syncs_per_week >= 2:
                patterns['deployment_frequency'] = 'moderate'
            else:
                patterns['deployment_frequency'] = 'low'

        if sync_activity['total_syncs'] > 0:
            patterns['deployment_stability'] = (
                sync_activity['successful_syncs'] / sync_activity['total_syncs']) * 100

        if sync_activity.get('by_instance'):
            max_syncs = 0
            for inst_name, inst_data in sync_activity['by_instance'].items():
                if inst_data['syncs'] > max_syncs:
                    max_syncs = inst_data['syncs']
                    patterns['primary_instance'] = inst_name

        return patterns

    def _analyze_instances_summary(self, instance_cache: Dict[str, Dict]) -> Dict[str, Any]:
        """Summary of each instance from cached data."""
        summary = {}
        for instance in self.instances:
            name = instance['name']
            cache = instance_cache.get(name, {})
            summary[name] = {
                'apps_managed': len(cache.get('apps_list', [])),
                'syncs_performed': 0,
                'total_projects': len(cache.get('projects', []))
            }
        return summary

    # ------------------------------------------------------------------
    # Event parsing
    # ------------------------------------------------------------------

    def _parse_sync_events(self, events: List[Dict], app_name: str) -> List[Dict[str, Any]]:
        """Convert ArgoCD application events into normalised sync records.

        Pairs ``OperationStarted`` events (which carry the initiating
        username) with ``OperationCompleted`` / ``OperationError`` events
        to produce records that mirror the structure derived from
        ``status.history``.
        """
        started_events: List[tuple] = []
        completion_events: List[tuple] = []

        for event in events:
            reason = event.get('reason', '')
            message = event.get('message', '')
            ts_str = (event.get('lastTimestamp')
                      or event.get('firstTimestamp')
                      or event.get('metadata', {}).get('creationTimestamp'))
            ts = parse_datetime_with_timezone(ts_str) if ts_str else None

            if reason == 'OperationStarted':
                user = self._extract_user_from_event_message(message)
                if ts:
                    started_events.append((ts, user))
            elif reason == 'OperationCompleted':
                if ts:
                    completion_events.append((ts, True, message))
            elif reason in ('OperationError', 'SyncError', 'OperationFailed'):
                if ts:
                    completion_events.append((ts, False, message))

        started_events.sort(key=lambda x: x[0])

        syncs: List[Dict[str, Any]] = []
        for sync_time, success, _message in completion_events:
            username = self._find_initiator(sync_time, started_events)
            syncs.append({
                'app': app_name,
                'time': sync_time,
                'username': username or '',
                'automated': not username,
                'success': success,
                'source': 'events',
            })

        return syncs

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sync_time(sync_entry: Dict[str, Any]) -> datetime | None:
        deploy_started = sync_entry.get('deployStartedAt')
        if deploy_started:
            t = parse_datetime_with_timezone(deploy_started)
            if t:
                return t

        for field in ('deployedAt', 'finishedAt'):
            value = sync_entry.get(field)
            if value:
                t = parse_datetime_with_timezone(value)
                if t:
                    return t
        return None

    @staticmethod
    def _user_matches_aliases(initiator: str, aliases: List[str]) -> bool:
        """Check whether *initiator* (from a sync record) matches any of *aliases*."""
        if not initiator:
            return False

        initiator_lower = initiator.lower()
        local_part = initiator.split('@')[0].lower() if '@' in initiator else None

        for alias in aliases:
            uname = alias.lower()
            if initiator_lower == uname:
                return True
            if local_part is not None and local_part == uname:
                return True
            if uname in initiator_lower:
                return True
        return False

    @staticmethod
    def _determine_sync_success(sync_entry: Dict[str, Any]) -> bool:
        sync_phase = sync_entry.get('phase', '').lower()
        if sync_phase:
            if sync_phase in ('succeeded', 'successful', 'synced'):
                return True
            if sync_phase in ('failed', 'error', 'degraded'):
                return False

        deployed_at = sync_entry.get('deployedAt')
        finished_at = sync_entry.get('finishedAt')
        sync_message = sync_entry.get('message', '').lower()

        if (deployed_at or finished_at) and 'error' not in sync_message and 'fail' not in sync_message:
            return True
        return False

    @staticmethod
    def _extract_user_from_event_message(message: str) -> Optional[str]:
        """Extract username from an ArgoCD event message.

        ArgoCD ``OperationStarted`` messages typically contain a phrase
        like ``"initiated by lukeskywalker"`` or
        ``"initiated by lukeskywalker@example.com"``.
        """
        match = re.search(r'initiated by\s+(\S+)', message, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _find_initiator(sync_time: datetime,
                        started_events: List[tuple]) -> Optional[str]:
        """Find the ``OperationStarted`` user closest to (and before) *sync_time*.

        Only considers started events within a 1-hour window before the
        completion event — wider gaps are unlikely to be the same
        operation.
        """
        best_user: Optional[str] = None
        best_delta: Optional[timedelta] = None

        for ts, username in started_events:
            if not username or not ts:
                continue
            delta = sync_time - ts
            if delta.total_seconds() < 0:
                continue
            if delta.total_seconds() > 3600:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_user = username

        return best_user

    @staticmethod
    def _deduplicate_syncs(syncs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate sync records across history and events.

        When the same sync appears in both ``status.history`` and the
        events stream the history record is preferred (it has richer
        structured data).  Two records are considered duplicates when
        they share the same app name and their timestamps round to the
        same minute.
        """
        history_keys: set = set()
        for s in syncs:
            if s['source'] == 'history' and s.get('time'):
                rounded = s['time'].replace(second=0, microsecond=0)
                history_keys.add((s['app'], rounded))

        result: List[Dict[str, Any]] = []
        for s in syncs:
            if s['source'] == 'history':
                result.append(s)
            elif s.get('time'):
                rounded = s['time'].replace(second=0, microsecond=0)
                if (s['app'], rounded) not in history_keys:
                    result.append(s)

        return result
