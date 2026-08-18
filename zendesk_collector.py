#!/usr/bin/env python3
"""
ZenDesk Metrics Collector
"""

from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Set
from utils import logger, create_session
from team import TeamMember


class ZenDeskCollector:
    def __init__(self, config: Dict[str, Any]):
        self.subdomain = config['subdomain']
        self.email = config['email']
        self.api_token = config['api_token']
        self.email_domain = config.get('email_domain', '')
        self.base_url = f"https://{self.subdomain}.zendesk.com"
        self.auth = (f"{self.email}/token", self.api_token)
        self.session = create_session()
        self._agent_id_cache: Dict[str, Optional[int]] = {}

    def collect_team_metrics(self, members: List[TeamMember],
                             start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Collect ZenDesk metrics using two-phase pattern.

        Phase 1: Fetch data per member from ZenDesk API.
        Phase 2: Analyze fetched data into metrics.

        A member may map to several ZenDesk aliases (e.g. accounts under different
        emails); tickets and comments across all of their agent IDs are combined.
        """
        team_metrics = {}

        for member in members:
            logger.info(f"Collecting ZenDesk metrics for {member.id}")

            # Phase 1: Resolve every alias to an agent id, then fetch tickets for all
            agent_ids = set()
            for alias in member.aliases_for('zendesk'):
                agent_id = self._resolve_agent_id(alias)
                if agent_id:
                    agent_ids.add(agent_id)

            if not agent_ids:
                logger.warning(f"Could not resolve any ZenDesk agent ID for {member.id}")
                team_metrics[member.id] = self._empty_metrics()
                continue

            tickets = {}
            for agent_id in agent_ids:
                for ticket in self._fetch_tickets(agent_id, start_date):
                    if ticket.get('id') is not None:
                        tickets[ticket['id']] = ticket
            tickets = list(tickets.values())
            ticket_details = self._fetch_ticket_details(tickets)

            # Phase 2: Analyze
            team_metrics[member.id] = self._analyze_metrics(tickets, ticket_details, agent_ids)

        return team_metrics

    # ------------------------------------------------------------------
    # Phase 1: Data fetching
    # ------------------------------------------------------------------

    def _resolve_agent_id(self, username: str) -> Optional[int]:
        """Resolve a username to a ZenDesk agent ID via user search."""
        if username in self._agent_id_cache:
            return self._agent_id_cache[username]

        email = f"{username}@{self.email_domain}"
        url = f"{self.base_url}/api/v2/users/search.json"
        params = {'query': email}

        try:
            response = self.session.get(url, auth=self.auth, params=params, timeout=15)
            if response.status_code == 200:
                data = response.json()
                users = data.get('users', [])
                for user in users:
                    if user.get('email', '').lower() == email.lower():
                        agent_id = user['id']
                        self._agent_id_cache[username] = agent_id
                        return agent_id
        except Exception as e:
            logger.debug(f"ZenDesk user search error for {username}: {e}")

        self._agent_id_cache[username] = None
        return None

    def _fetch_tickets(self, agent_id: int, start_date: datetime) -> List[Dict[str, Any]]:
        """Fetch tickets assigned to an agent since start_date (paginated)."""
        date_str = start_date.strftime('%Y-%m-%d')
        query = f"type:ticket assignee:{agent_id} updated>{date_str}"
        url = f"{self.base_url}/api/v2/search.json"
        params = {'query': query}

        tickets = []
        while url:
            try:
                response = self.session.get(url, auth=self.auth, params=params, timeout=15)
                if response.status_code != 200:
                    logger.warning(f"ZenDesk search failed: {response.status_code}")
                    break
                data = response.json()
                tickets.extend(data.get('results', []))
                url = data.get('next_page')
                params = None  # next_page URL includes params
            except Exception as e:
                logger.debug(f"ZenDesk ticket fetch error: {e}")
                break

        return tickets

    def _fetch_ticket_details(self, tickets: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """Fetch metrics, satisfaction ratings, comments, and audits per ticket."""
        details = {}

        for ticket in tickets:
            ticket_id = ticket.get('id')
            if not ticket_id:
                continue

            detail = {
                'metrics': None,
                'satisfaction_ratings': [],
                'comments': [],
                'audits': [],
            }

            # Ticket metrics
            try:
                resp = self.session.get(
                    f"{self.base_url}/api/v2/tickets/{ticket_id}/metrics.json",
                    auth=self.auth, timeout=15)
                if resp.status_code == 200:
                    detail['metrics'] = resp.json().get('ticket_metric', {})
            except Exception as e:
                logger.debug(f"ZenDesk metrics fetch error for ticket {ticket_id}: {e}")

            # Satisfaction ratings
            try:
                resp = self.session.get(
                    f"{self.base_url}/api/v2/tickets/{ticket_id}/satisfaction_ratings.json",
                    auth=self.auth, timeout=15)
                if resp.status_code == 200:
                    detail['satisfaction_ratings'] = resp.json().get('satisfaction_ratings', [])
            except Exception as e:
                logger.debug(f"ZenDesk satisfaction fetch error for ticket {ticket_id}: {e}")

            # Comments
            try:
                resp = self.session.get(
                    f"{self.base_url}/api/v2/tickets/{ticket_id}/comments.json",
                    auth=self.auth, timeout=15)
                if resp.status_code == 200:
                    detail['comments'] = resp.json().get('comments', [])
            except Exception as e:
                logger.debug(f"ZenDesk comments fetch error for ticket {ticket_id}: {e}")

            # Audits
            try:
                resp = self.session.get(
                    f"{self.base_url}/api/v2/tickets/{ticket_id}/audits.json",
                    auth=self.auth, timeout=15)
                if resp.status_code == 200:
                    detail['audits'] = resp.json().get('audits', [])
            except Exception as e:
                logger.debug(f"ZenDesk audits fetch error for ticket {ticket_id}: {e}")

            details[ticket_id] = detail

        return details

    # ------------------------------------------------------------------
    # Phase 2: Analysis (pure computation)
    # ------------------------------------------------------------------

    def _analyze_metrics(self, tickets: List[Dict[str, Any]],
                         ticket_details: Dict[int, Dict[str, Any]],
                         agent_ids: Optional[Set[int]] = None) -> Dict[str, Any]:
        """Analyze fetched ticket data into structured metrics."""
        return {
            'ticket_activity': self._analyze_ticket_activity(tickets),
            'resolution_metrics': self._analyze_resolution_metrics(tickets, ticket_details),
            'customer_satisfaction': self._analyze_customer_satisfaction(ticket_details),
            'ticket_distribution': self._analyze_ticket_distribution(tickets),
            'collaboration': self._analyze_collaboration(ticket_details, agent_ids),
        }

    def _analyze_ticket_activity(self, tickets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Count tickets by status."""
        activity = {
            'assigned': len(tickets),
            'solved': 0,
            'open': 0,
            'pending': 0,
            'on_hold': 0,
            'reopened': 0,
        }

        for ticket in tickets:
            status = ticket.get('status', '').lower()
            if status == 'solved' or status == 'closed':
                activity['solved'] += 1
            elif status == 'open' or status == 'new':
                activity['open'] += 1
            elif status == 'pending':
                activity['pending'] += 1
            elif status == 'hold':
                activity['on_hold'] += 1

        # Count reopened via tags or via_followup
        for ticket in tickets:
            tags = ticket.get('tags', [])
            if 'reopened' in tags:
                activity['reopened'] += 1

        return activity

    def _analyze_resolution_metrics(self, tickets: List[Dict[str, Any]],
                                     ticket_details: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate average resolution and first response times."""
        resolution_hours = []
        first_response_hours = []

        for ticket in tickets:
            ticket_id = ticket.get('id')
            detail = ticket_details.get(ticket_id, {})
            metrics = detail.get('metrics')
            if not metrics:
                continue

            # Resolution time
            reply_time = metrics.get('reply_time_in_minutes', {})
            if isinstance(reply_time, dict):
                calendar_mins = reply_time.get('calendar', 0)
            else:
                calendar_mins = reply_time or 0
            if calendar_mins and calendar_mins > 0:
                first_response_hours.append(calendar_mins / 60.0)

            # Full resolution time
            full_resolution = metrics.get('full_resolution_time_in_minutes', {})
            if isinstance(full_resolution, dict):
                res_mins = full_resolution.get('calendar', 0)
            else:
                res_mins = full_resolution or 0
            if res_mins and res_mins > 0:
                resolution_hours.append(res_mins / 60.0)

        return {
            'avg_resolution_hours': round(sum(resolution_hours) / len(resolution_hours), 2) if resolution_hours else 0,
            'avg_first_response_hours': round(sum(first_response_hours) / len(first_response_hours), 2) if first_response_hours else 0,
        }

    def _analyze_customer_satisfaction(self, ticket_details: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze CSAT ratings across all tickets."""
        scores = []
        good = 0
        bad = 0

        for detail in ticket_details.values():
            for rating in detail.get('satisfaction_ratings', []):
                score = rating.get('score')
                if score == 'good':
                    good += 1
                    scores.append(1.0)
                elif score == 'bad':
                    bad += 1
                    scores.append(0.0)

        return {
            'avg_csat_score': round(sum(scores) / len(scores), 2) if scores else 0,
            'csat_responses': len(scores),
            'good_ratings': good,
            'bad_ratings': bad,
        }

    def _analyze_ticket_distribution(self, tickets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze ticket type and priority distributions."""
        type_dist: Dict[str, int] = {}
        priority_dist: Dict[str, int] = {}

        for ticket in tickets:
            t_type = ticket.get('type') or 'unknown'
            type_dist[t_type] = type_dist.get(t_type, 0) + 1

            priority = ticket.get('priority') or 'unknown'
            priority_dist[priority] = priority_dist.get(priority, 0) + 1

        return {
            'type_distribution': type_dist,
            'priority_distribution': priority_dist,
        }

    def _analyze_collaboration(self, ticket_details: Dict[int, Dict[str, Any]],
                               agent_ids: Optional[Set[int]] = None) -> Dict[str, Any]:
        """Count internal notes, public replies, and total comments.

        Tracks both overall ticket comment counts and agent-specific counts
        (comments authored by the team member, across all of their agent IDs).
        """
        agent_ids = agent_ids or set()
        internal_notes = 0
        public_replies = 0
        total_comments = 0
        agent_internal_notes = 0
        agent_public_replies = 0
        agent_total_comments = 0

        for detail in ticket_details.values():
            for comment in detail.get('comments', []):
                total_comments += 1
                is_public = comment.get('public', True)
                if is_public:
                    public_replies += 1
                else:
                    internal_notes += 1

                # Track agent-specific comments
                if comment.get('author_id') in agent_ids:
                    agent_total_comments += 1
                    if is_public:
                        agent_public_replies += 1
                    else:
                        agent_internal_notes += 1

        return {
            'internal_notes': internal_notes,
            'public_replies': public_replies,
            'total_comments': total_comments,
            'agent_internal_notes': agent_internal_notes,
            'agent_public_replies': agent_public_replies,
            'agent_total_comments': agent_total_comments,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_metrics() -> Dict[str, Any]:
        """Return an empty metrics structure for users not found in ZenDesk."""
        return {
            'ticket_activity': {
                'assigned': 0, 'solved': 0, 'open': 0,
                'pending': 0, 'on_hold': 0, 'reopened': 0,
            },
            'resolution_metrics': {
                'avg_resolution_hours': 0, 'avg_first_response_hours': 0,
            },
            'customer_satisfaction': {
                'avg_csat_score': 0, 'csat_responses': 0,
                'good_ratings': 0, 'bad_ratings': 0,
            },
            'ticket_distribution': {
                'type_distribution': {}, 'priority_distribution': {},
            },
            'collaboration': {
                'internal_notes': 0, 'public_replies': 0, 'total_comments': 0,
                'agent_internal_notes': 0, 'agent_public_replies': 0, 'agent_total_comments': 0,
            },
        }
