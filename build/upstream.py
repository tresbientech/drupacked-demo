#!/usr/bin/env python3
"""Moves every pin of this site to its newest version inside its line, and prints
what it did as a Markdown summary. It edits the working tree and never commits.

    python3 build/upstream.py
"""

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/build.yml"
COMPOSER = "composer.json"
SITE = "drupack.yml"

# The release Drupack's own self-update reads: the one GitHub marks latest.
DRUPACK_RELEASE = "https://github.com/tresbientech/drupack/releases/latest/download/release.json"
DRUPACK_SOURCE = "https://raw.githubusercontent.com/tresbientech/drupack/"
NODE_RELEASES = "https://nodejs.org/dist/index.json"
DRUPAL_METADATA = "https://packages.drupal.org/files/packages/8/p2/"
PACKAGIST_METADATA = "https://repo.packagist.org/p2/"

# Each pin's line in its file, its value captured.
DRUPACK_PIN = r"build\.yml@(\S+)$"
PLATFORM_PIN = r'"platform": \{"php": "([^"]+)"\}'
NODE_PIN = r"^node: (.+)$"
# The PHP a Drupack release compiles, in its runtime/builder-inputs.sh.
DRUPACK_PHP = r"^php_version=(\d+\.\d+\.\d+)$"
STABLE_TAG = re.compile(r"v\d+\.\d+\.\d+")

# Exact pins the bumper leaves alone, each with the reason a newer release breaks
# the site. Deleting an entry lets the next run bump the package.
HOLDS = {
    "twig/twig": "under 3.30 cron fails with a TypeError in easy_email's compiled template",
}

RELEASE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)(\d+))?", re.IGNORECASE)
STABILITY = {"alpha": 0, "beta": 1, "rc": 2, None: 3}


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode()


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def release_key(version):
    """Orders a release by number, then stability, then prerelease number; None for any other version."""
    match = RELEASE.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, stability, number = match.groups()
    return (int(major), int(minor), int(patch), STABILITY[stability and stability.lower()], int(number or 0))


class Summary:
    def __init__(self):
        self.rows = []
        self.notes = []

    def row(self, source, current, found, action):
        self.rows.append((source, current, found, action))

    def render(self):
        lines = ["| Source | Current | Found | Action |", "|---|---|---|---|"]
        lines += [f"| {' | '.join(row)} |" for row in self.rows]
        return "\n".join(lines + [""] + self.notes) + "\n"


def read(path, pattern):
    """The single value `pattern` captures in `path`."""
    values = re.findall(pattern, (ROOT / path).read_text(), re.MULTILINE)
    if len(values) != 1:
        sys.exit(f"{path}: {pattern} matches {len(values)} times, not once")
    return values[0]


def rewrite(path, pattern, value):
    """Replaces the one value `pattern` captures in `path` with `value`."""
    file = ROOT / path
    text = file.read_text()
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        sys.exit(f"{path}: {pattern} matches {len(matches)} times, not once")
    start, end = matches[0].span(1)
    file.write_text(text[:start] + value + text[end:])


def drupack(summary):
    current = read(WORKFLOW, DRUPACK_PIN)
    # GitHub's answers are remote input.
    newest = json.loads(fetch(DRUPACK_RELEASE))["version"]
    if release_key(newest) is None:
        sys.exit(f"Drupack's latest release names {newest!r}, not a version")
    if release_key(newest) <= release_key(current):
        summary.row("Drupack", current, newest, "current")
        return
    php = re.findall(DRUPACK_PHP, fetch(f"{DRUPACK_SOURCE}{newest}/runtime/builder-inputs.sh"), re.MULTILINE)
    if len(php) != 1:
        sys.exit(f"Drupack {newest} names {len(php)} PHP versions, not one")
    rewrite(WORKFLOW, DRUPACK_PIN, newest)
    summary.row("Drupack", current, newest, "bumped")
    # Composer resolves the lock against the PHP the pinned Drupack runs.
    platform = read(COMPOSER, PLATFORM_PIN)
    if php[0] != platform:
        rewrite(COMPOSER, PLATFORM_PIN, php[0])
        summary.row("PHP platform", platform, php[0], "bumped")


def node(summary):
    current = read(SITE, NODE_PIN)
    line = current.split(".")[0]
    # nodejs.org's index is remote input; lts is false or the line's name.
    versions = [release["version"][1:] for release in json.loads(fetch(NODE_RELEASES))
                if release["lts"] and STABLE_TAG.fullmatch(release["version"])]
    newest = max((v for v in versions if v.split(".")[0] == line), key=version_key)
    above = max(versions, key=version_key)
    if above.split(".")[0] != line:
        summary.notes.append(f"MAJOR Node {above} LTS is out, outside the {line} line")
    if version_key(newest) <= version_key(current):
        summary.row("Node", current, newest, "current")
        return
    rewrite(SITE, NODE_PIN, newest)
    summary.row("Node", current, newest, "bumped")


def metadata(name):
    """`name`'s Composer metadata entries, from drupal.org first and Packagist after, as the site's
    repositories order them."""
    bases = [DRUPAL_METADATA, PACKAGIST_METADATA] if name.startswith("drupal/") else [PACKAGIST_METADATA]
    for base in bases:
        try:
            return json.loads(fetch(f"{base}{name}.json"))["packages"][name]
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    sys.exit(f"{name}: no repository lists it")


def releases(name):
    """Every version Composer metadata lists for `name`, as written there."""
    return [entry["version"] for entry in metadata(name)]


def newest_release(current, versions):
    """The newest of `versions` in `current`'s major at its stability or better, and the newest above that major."""
    floor = release_key(current)
    keyed = [(release_key(version), version.removeprefix("v")) for version in versions]
    eligible = [(key, version) for key, version in keyed if key is not None and key[3] >= floor[3]]
    newest = max(entry for entry in eligible if entry[0][0] == floor[0])
    above = [entry for entry in eligible if entry[0][0] > floor[0]]
    return newest, max(above) if above else None


def composer_update():
    subprocess.run(["composer", "update", "--no-install", "--no-interaction", "--no-progress", "--no-audit"],
                   cwd=ROOT, check=True)


def locked_versions():
    lock = json.loads((ROOT / "composer.lock").read_text())
    return {package["name"]: package["version"] for package in lock["packages"] + lock["packages-dev"]}


def composer(summary):
    for name, constraint in json.loads((ROOT / COMPOSER).read_text())["require"].items():
        if name in HOLDS:
            newest = newest_release(constraint, releases(name))[0][1]
            summary.notes.append(f"HELD {name} {constraint} ({newest}): {HOLDS[name]}")
            continue
        # A range moves with composer update; a dev branch pin moves only by hand.
        if release_key(constraint) is None:
            continue
        (key, newest), above = newest_release(constraint, releases(name))
        if above is not None:
            summary.notes.append(f"MAJOR {name} {above[1]} is out, outside the {key[0]}.x line")
        if key <= release_key(constraint):
            summary.row(name, constraint, newest, "current")
            continue
        rewrite(COMPOSER, rf'^\s*"{re.escape(name)}": "([^"]+)"', newest)
        summary.row(name, constraint, newest, "bumped")
    before = locked_versions()
    composer_update()
    after = locked_versions()
    bumped = {row[0] for row in summary.rows}
    for name in sorted(after):
        if before.get(name) != after[name] and name not in bumped:
            summary.row(name, before.get(name, "none"), after[name], "locked")


def main():
    summary = Summary()
    drupack(summary)
    node(summary)
    composer(summary)
    sys.stdout.write(summary.render())


if __name__ == "__main__":
    main()
