#!/usr/bin/env python3
"""Run every test_*.py in this directory and summarise the results.

Each test runs in its own interpreter so a crash or leaked matplotlib state in one
cannot affect another. Exits non-zero if any test fails, so this is usable as a
pre-commit or CI step:

    python tests/run_all.py
"""

import os
import subprocess
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    scripts = sorted(name for name in os.listdir(TESTS_DIR)
                     if name.startswith('test_') and name.endswith('.py'))

    if not scripts:
        print('No test_*.py files found.')
        return 1

    verbose = '-v' in sys.argv or '--verbose' in sys.argv
    failed = []

    for script in scripts:
        path = os.path.join(TESTS_DIR, script)
        result = subprocess.run([sys.executable, path], capture_output=not verbose,
                                text=True)
        status = 'PASS' if result.returncode == 0 else 'FAIL'
        print(f'{status}  {script}')

        if result.returncode != 0:
            failed.append(script)
            if not verbose:
                # Show the detail only for failures, so a green run stays quiet.
                print((result.stdout or '').rstrip())
                if result.stderr:
                    print((result.stderr or '').rstrip(), file=sys.stderr)

    print()
    if failed:
        print(f'{len(failed)} of {len(scripts)} test files failed: {", ".join(failed)}')
        return 1
    print(f'All {len(scripts)} test files passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
