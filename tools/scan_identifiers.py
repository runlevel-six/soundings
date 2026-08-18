#!/usr/bin/env python3
"""Refuse to publish anything that names a real person.

The repository is public; the data it reads is not. Everything real lives in
files git ignores — the roster in ``team.json``, credentials and hostnames in
``.env``, people and project prose in the Jira export — while the committed tree
is supposed to carry placeholders only.

This scanner derives its search terms *from those private files* rather than from
a hand-maintained list, then greps every tracked file for them. That direction
matters: a hand-written list only ever catches the names someone remembered to
add to it, which is exactly how a real name survives a sanitization pass. Add a
teammate to ``team.json`` today and they are scanned for today.

Usage::

    python tools/scan_identifiers.py                # the working tree
    python tools/scan_identifiers.py --rev HEAD     # one commit
    python tools/scan_identifiers.py --list-terms   # what it is looking for

Exits non-zero when anything is found, so it works as a pre-push hook (see
``.githooks/pre-push``) or a CI step. With none of the private files present -- a
fresh clone, or CI -- there is almost nothing to derive, and it passes trivially;
it is a guard for the machine that holds the real data, not a proof of safety
anywhere else.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit

# Files whose real-world content is deliberate. The author's own copyright is
# not a leak; a teammate's name anywhere else is.
ALLOWED_PATHS = {'LICENSE'}

# Terms shorter than this produce more noise than signal (initials, short
# project keys, two-letter aliases). Anything shorter has to be caught by eye.
MIN_TERM_LENGTH = 4

# Generic words that turn up as environment values or surname collisions and
# would otherwise match ordinary prose. Deliberately generic: nothing here may
# hint at the real deployment.
STOPWORDS = {
    'admin', 'change', 'cloud', 'default', 'false', 'file', 'json', 'local',
    'main', 'master', 'none', 'null', 'password', 'path', 'prod', 'staging',
    'team', 'test', 'token', 'true', 'user', 'username', 'value', 'your',
}

# Environment variables whose *values* identify a place or a person. Windows,
# timezones and file paths are configuration, not identity.
IDENTIFYING_ENV_SUFFIXES = (
    '_URL', '_HOST', '_HOSTNAME', '_EMAIL', '_ORG', '_ORG_NAME', '_SUBDOMAIN',
    '_USERNAME', '_PASSWORD', '_TOKEN', '_API_KEY', '_KEY', '_DOMAIN',
    '_PROJECT_KEY', '_ACCOUNT',
)

JIRA_PEOPLE_COLUMNS = {'Assignee', 'Reporter', 'Creator', 'Project lead',
                       'Watchers', 'Project name'}


def repo_root() -> str:
    return subprocess.check_output(['git', 'rev-parse', '--show-toplevel'],
                                   text=True).strip()


def _split_words(value: str):
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'._-]{2,}", value)


def terms_from_team_files(root: str) -> set:
    """Names, ids and per-source aliases from any roster file but the example."""
    terms = set()
    for name in sorted(os.listdir(root)):
        if not (name == 'team.json' or (name.startswith('team.')
                                        and name.endswith('.json'))):
            continue
        if name == 'team.example.json':
            continue
        try:
            roster = json.load(open(os.path.join(root, name)))
        except (ValueError, OSError):
            continue
        for member in roster:
            terms.add(member.get('id', ''))
            display = member.get('display_name', '')
            terms.add(display)
            terms.update(display.split())
            for aliases in member.get('aliases', {}).values():
                terms.update(aliases)
    return terms


def _env_values(path: str) -> dict:
    values = {}
    try:
        handle = open(path, encoding='utf-8', errors='replace')
    except OSError:
        return values
    with handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip().strip('"\'')
    return values


def terms_from_env(root: str) -> set:
    """Hostnames, org slugs, logins and secrets from the real .env files.

    Values that also appear in the committed .env.example are placeholders by
    definition and are dropped, so the template never flags itself.
    """
    placeholders = set(_env_values(os.path.join(root, '.env.example')).values())
    terms = set()
    for name in sorted(os.listdir(root)):
        if not (name == '.env' or name.startswith('.env.')):
            continue
        if name == '.env.example':
            continue
        for key, value in _env_values(os.path.join(root, name)).items():
            if not value or value in placeholders:
                continue
            if value.startswith(('.', '/', '~')):        # a local path
                continue
            if not key.upper().endswith(IDENTIFYING_ENV_SUFFIXES):
                continue
            terms.add(value)
            if '://' in value:
                host = urlsplit(value).hostname or ''
                terms.add(host)
                terms.update(host.split('.'))
            elif '@' in value:
                local, _, domain = value.partition('@')
                terms.add(local)
                terms.add(domain)
                terms.update(domain.split('.'))
            else:
                terms.update(_split_words(value))
    return terms


def terms_from_exports(root: str, row_limit: int = 50000) -> set:
    """People and organisation names from any local issue-tracker export."""
    terms = set()
    for name in sorted(os.listdir(root)):
        if not name.endswith('_export.csv'):
            continue
        try:
            handle = open(os.path.join(root, name), newline='',
                          encoding='utf-8', errors='replace')
        except OSError:
            continue
        with handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                continue
            people = [i for i, column in enumerate(header)
                      if column in JIRA_PEOPLE_COLUMNS]
            prose = [i for i, column in enumerate(header)
                     if column in {'Project description'}]
            for count, row in enumerate(reader):
                if count >= row_limit:
                    break
                for index in people:
                    if index < len(row) and row[index].strip():
                        terms.add(row[index].strip())
                for index in prose:
                    if index < len(row):
                        # Organisation names live in prose. Only multi-word
                        # proper nouns; a lone capitalised word is usually just
                        # the start of a sentence.
                        terms.update(re.findall(
                            r"\b[A-Z][A-Za-z']{2,}"
                            r"(?:\s+(?:of|and|for|the)?\s*[A-Z][A-Za-z']{2,}){1,4}",
                            row[index])[:40])
    return terms


def terms_from_git(root: str) -> set:
    """The committer's own identity and the remote they publish to."""
    terms = set()
    config = subprocess.run(['git', 'config', '--list'], capture_output=True,
                            text=True, cwd=root).stdout
    for line in config.splitlines():
        if line.startswith(('user.name=', 'user.email=')):
            value = line.split('=', 1)[1]
            terms.add(value)
            terms.update(_split_words(value))
    remotes = subprocess.run(['git', 'remote', '-v'], capture_output=True,
                             text=True, cwd=root).stdout
    terms.update(re.findall(r'[:/]([A-Za-z0-9._-]+)/[A-Za-z0-9._-]+\.git', remotes))
    return terms


def build_terms(root: str) -> list:
    terms = (terms_from_team_files(root) | terms_from_env(root)
             | terms_from_exports(root) | terms_from_git(root))
    cleaned = set()
    for term in terms:
        term = term.strip()
        if len(term) < MIN_TERM_LENGTH or term.lower() in STOPWORDS:
            continue
        if not re.search(r'[A-Za-z]', term):
            continue
        cleaned.add(term)
    # Longest first, so a report shows the most specific match on a line.
    return sorted(cleaned, key=lambda term: (-len(term), term.lower()))


def tracked_files(root: str, rev) -> list:
    if rev:
        command = ['git', 'ls-tree', '-r', '--name-only', rev]
    else:
        command = ['git', 'ls-files']
    return [path for path in subprocess.check_output(command, text=True,
                                                     cwd=root).split('\n') if path]


def file_text(root: str, path: str, rev) -> str:
    if rev:
        return subprocess.run(['git', 'show', f'{rev}:{path}'], capture_output=True,
                              text=True, cwd=root).stdout
    try:
        with open(os.path.join(root, path), encoding='utf-8', errors='replace') as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return ''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--rev', help='scan a commit instead of the working tree')
    parser.add_argument('--list-terms', action='store_true',
                        help='print the derived terms and exit')
    parser.add_argument('--quiet', action='store_true',
                        help='print findings only, no summary')
    args = parser.parse_args()

    root = repo_root()
    terms = build_terms(root)

    if args.list_terms:
        for term in terms:
            print(term)
        return 0

    if not args.quiet:
        where = args.rev or 'the working tree'
        print(f'scanning {where} for {len(terms)} identifier(s) '
              'derived from local private files')

    patterns = [(term, re.compile(rf'(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])',
                                  re.IGNORECASE)) for term in terms]

    findings = 0
    for path in tracked_files(root, args.rev):
        if path in ALLOWED_PATHS:
            continue
        text = file_text(root, path, args.rev)
        if not text:
            continue
        lines = text.splitlines()
        for term, pattern in patterns:
            for match in pattern.finditer(text):
                line_no = text.count('\n', 0, match.start()) + 1
                findings += 1
                prefix = f'{args.rev}:' if args.rev else ''
                print(f'{prefix}{path}:{line_no}: [{term}] '
                      f'{lines[line_no - 1].strip()[:140]}')

    if findings:
        print(f'{findings} identifier reference(s) found — do not publish this.')
        return 1
    if not args.quiet:
        print('clean')
    return 0


if __name__ == '__main__':
    sys.exit(main())
