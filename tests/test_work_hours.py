#!/usr/bin/env python3
"""Out-of-hours classification: per-member timezones and the business-hours window.

The original implementation bucketed on UTC, which inverted the result for a US
team — a 2pm Central page was recorded as after-hours and a 4am one as business
hours. These tests pin the corrected behaviour.
"""

import json
import tempfile
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import _support  # noqa: F401 - sets sys.path and the matplotlib backend
from _support import Results

from opsgenie_collector import OpsGenieCollector
from team import load_team_from_file
from utils import (DEFAULT_BUSINESS_HOURS, classify_work_time, parse_business_hours,
                   resolve_timezone)

r = Results('Out-of-hours classification')

CHICAGO = ZoneInfo('America/Chicago')
DENVER = ZoneInfo('America/Denver')
NEW_YORK = ZoneInfo('America/New_York')


def utc_ms(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute,
                        tzinfo=timezone.utc).timestamp() * 1000)


def local_ms(tz, year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp() * 1000)


r.section('the regression: UTC bucketing inverted a US working day')
# 2026-07-15 is a Wednesday. 14:00 CDT = 19:00 UTC.
two_pm_central = utc_ms(2026, 7, 15, 19)
r.equals('UTC called a 2pm Central page after-hours (the old, wrong answer)',
         classify_work_time(two_pm_central, timezone.utc), 'after_hours')
r.equals('Central calls it business hours',
         classify_work_time(two_pm_central, CHICAGO), 'business')

four_am_central = utc_ms(2026, 7, 15, 9)   # 04:00 CDT
r.equals('UTC called a 4am Central page business hours (also wrong)',
         classify_work_time(four_am_central, timezone.utc), 'business')
r.equals('Central calls it after-hours',
         classify_work_time(four_am_central, CHICAGO), 'after_hours')

r.section('one instant, three timezones')
instant = utc_ms(2026, 7, 16, 1, 30)   # Thu 01:30 UTC = Wed evening in the US
for tz, label in [(CHICAGO, 'Chicago 20:30'), (DENVER, 'Denver 19:30'),
                  (NEW_YORK, 'New York 21:30')]:
    r.equals(f'{label} is after-hours', classify_work_time(instant, tz), 'after_hours')

r.section('default window boundaries (08:00 inclusive, 18:00 exclusive)')
r.equals('08:00 local is business',
         classify_work_time(local_ms(CHICAGO, 2026, 7, 15, 8), CHICAGO), 'business')
r.equals('07:59 local is after-hours',
         classify_work_time(local_ms(CHICAGO, 2026, 7, 15, 7, 59), CHICAGO), 'after_hours')
r.equals('17:59 local is business',
         classify_work_time(local_ms(CHICAGO, 2026, 7, 15, 17, 59), CHICAGO), 'business')
r.equals('18:00 local is after-hours',
         classify_work_time(local_ms(CHICAGO, 2026, 7, 15, 18), CHICAGO), 'after_hours')

r.section('weekend takes precedence, so counts never overlap')
saturday_night = local_ms(CHICAGO, 2026, 7, 18, 3)
r.equals('Saturday 03:00 local is weekend, not after_hours',
         classify_work_time(saturday_night, CHICAGO), 'weekend')

r.section('timezone can change the day, not just the hour')
sunday_late = utc_ms(2026, 7, 20, 4)   # Sun 23:00 CDT = Mon 04:00 UTC
r.equals('UTC calls it a weekday night',
         classify_work_time(sunday_late, timezone.utc), 'after_hours')
r.equals('Central correctly calls it the weekend',
         classify_work_time(sunday_late, CHICAGO), 'weekend')

r.section('DST does not shift the local working day')
r.equals('14:00 CST is business',
         classify_work_time(local_ms(CHICAGO, 2026, 1, 14, 14), CHICAGO), 'business')
r.equals('14:00 CDT is business',
         classify_work_time(local_ms(CHICAGO, 2026, 7, 15, 14), CHICAGO), 'business')

r.section('configurable window with minute precision (09:00-17:30)')
custom_hours = parse_business_hours('09:00-17:30')
r.equals('parses to time objects', custom_hours, (time(9, 0), time(17, 30)))
for hour, minute, expected in [(8, 59, 'after_hours'), (9, 0, 'business'),
                               (12, 0, 'business'), (17, 29, 'business'),
                               (17, 30, 'after_hours'), (18, 0, 'after_hours'),
                               (23, 0, 'after_hours')]:
    stamp = local_ms(NEW_YORK, 2026, 7, 15, hour, minute)
    r.equals(f'{hour:02d}:{minute:02d} ET is {expected}',
             classify_work_time(stamp, NEW_YORK, custom_hours), expected)

# 17:45 falls inside the default window but outside the narrower custom one,
# which is the whole point of making the window configurable.
late_afternoon_et = local_ms(NEW_YORK, 2026, 7, 15, 17, 45)
r.equals('17:45 ET is business under the 08:00-18:00 default',
         classify_work_time(late_afternoon_et, NEW_YORK, DEFAULT_BUSINESS_HOURS), 'business')
r.equals('17:45 ET is after-hours under 09:00-17:30',
         classify_work_time(late_afternoon_et, NEW_YORK, custom_hours), 'after_hours')

r.section('malformed windows are rejected, never silently defaulted')
for bad in ['8-16', '08:00', '09:00-17:30-20:00', '25:00-26:00', '09:99-17:30',
            '17:30-09:00', '09:00-09:00', 'morning-evening']:
    r.raises(f'{bad!r} rejected', lambda spec=bad: parse_business_hours(spec))
r.equals('empty spec falls back to the default',
         parse_business_hours(''), DEFAULT_BUSINESS_HOURS)
r.equals('None falls back to the default',
         parse_business_hours(None), DEFAULT_BUSINESS_HOURS)

r.section('roster timezone parsing')
path = tempfile.mktemp(suffix='.json')
with open(path, 'w') as f:
    json.dump([
        {'id': 'a', 'display_name': 'A', 'timezone': 'America/Chicago',
         'aliases': {'default': ['a']}},
        {'id': 'b', 'display_name': 'B', 'aliases': {'default': ['b']}},
    ], f)
members = {m.id: m for m in load_team_from_file(path)}
r.equals('timezone is read', members['a'].timezone, 'America/Chicago')
r.equals('absent timezone stays None', members['b'].timezone, None)

with open(path, 'w') as f:
    json.dump([{'id': 'c', 'display_name': 'C', 'timezone': 'America/Chicgo',
                'aliases': {'default': ['c']}}], f)
r.raises('a misspelled zone fails roster loading',
         lambda: load_team_from_file(path), ValueError, 'not a known IANA')

r.section('resolve_timezone fallbacks')
r.check('None yields UTC', resolve_timezone(None) is timezone.utc)
r.check('unknown name yields UTC', resolve_timezone('Not/AZone') is timezone.utc)
r.equals('known name yields that zone',
         str(resolve_timezone('America/Chicago')), 'America/Chicago')
r.check('"Americas/New_York" is not a real zone (the plural trap)',
        resolve_timezone('Americas/New_York') is timezone.utc)

r.section('the configured window reaches the metric')
late_afternoon = local_ms(NEW_YORK, 2026, 7, 15, 17, 45)
alerts = [{'id': 'y1', 'priority': 'P2', 'createdAt': late_afternoon}]
logs = {'y1': [{'type': 'acknowledge', 'owner': 'a@example.com',
                'log': 'Acknowledged', 'createdAt': late_afternoon + 60000}]}
collector = OpsGenieCollector({'api_key': 'k', 'email_domain': 'example.com',
                               'default_timezone': 'America/New_York',
                               'business_hours': '09:00-17:30'})
burden = collector._analyze_metrics(alerts, logs, {'a@example.com'},
                                    NEW_YORK)['on_call_burden']
r.equals('a 17:45 ET page counts as after-hours', burden['after_hours_alerts'], 1)
r.equals('and not as weekend', burden['weekend_alerts'], 0)

default_collector = OpsGenieCollector({'api_key': 'k', 'email_domain': 'example.com'})
r.equals('an unset window uses the default',
         default_collector.business_hours, DEFAULT_BUSINESS_HOURS)
r.equals('an unset timezone falls back to UTC', default_collector.default_timezone, 'UTC')

r.exit()
