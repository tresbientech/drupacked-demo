#!/usr/bin/env python3
"""Prints the version the next release takes: YEAR.MONTH.N, where N counts the
month's releases from 1. The month is the current one, in UTC.

    python3 build/next-version.py
"""

import re
import subprocess
import sys
from datetime import datetime, timezone


def next_version(tags, today):
    month = f"{today:%Y.%m}"
    counters = [int(tag.rsplit(".", 1)[1]) for tag in tags if re.fullmatch(rf"{re.escape(month)}\.\d+", tag)]
    return f"{month}.{max(counters, default=0) + 1}"


def main():
    tags = subprocess.run(["git", "tag", "--list"], check=True, capture_output=True, text=True).stdout.split()
    sys.stdout.write(next_version(tags, datetime.now(timezone.utc)) + "\n")


if __name__ == "__main__":
    main()
