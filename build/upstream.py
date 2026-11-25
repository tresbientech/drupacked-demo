#!/usr/bin/env python3
"""Moves every pin of this site to its newest version inside its line, and prints
what it did as a Markdown summary. It edits the working tree and never commits.

    python3 build/upstream.py
"""

import atexit
import functools
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/build.yml"
COMPOSER = "composer.json"
SITE = "drupack.yml"

# The release Drupack's own self-update reads: the one GitHub marks latest.
DRUPACK_RELEASE = "https://github.com/tresbientech/drupack/releases/latest/download/release.json"
DRUPACK_DOWNLOADS = "https://github.com/tresbientech/drupack/releases/download/"
# The forge runner, which runs the pinned engine's Composer.
ENGINE_TARGET = "linux-amd64"
NODE_RELEASES = "https://nodejs.org/dist/index.json"
DRUPAL_METADATA = "https://packages.drupal.org/files/packages/8/p2/"
PACKAGIST_METADATA = "https://repo.packagist.org/p2/"

# Each pin's line in its file, its value captured.
DRUPACK_PIN = r"build\.yml@(\S+)$"
NODE_PIN = r"^node: (.+)$"
STABLE_TAG = re.compile(r"v\d+\.\d+\.\d+")

# Exact pins the bumper leaves alone, each with the reason a newer release breaks
# the site. Deleting an entry lets the next run bump the package.
HOLDS = {}

# The verdicts drupatch's service gives a patch.
VERDICTS = {"applies", "merged", "conflicts", "unknown"}

RELEASE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)(\d+))?", re.IGNORECASE)
STABILITY = {"alpha": 0, "beta": 1, "rc": 2, None: 3}


def download(url):
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def fetch(url):
    return download(url).decode()


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
    rewrite(WORKFLOW, DRUPACK_PIN, newest)
    summary.row("Drupack", current, newest, "bumped")


def engine(directory):
    """The pinned Drupack engine for ENGINE_TARGET, downloaded into `directory` and checked."""
    version = read(WORKFLOW, DRUPACK_PIN)
    # GitHub's answers are remote input.
    assets = [asset for asset in json.loads(fetch(f"{DRUPACK_DOWNLOADS}{version}/release.json"))["assets"]
              if asset["name"] == "drupack" and asset["target"] == ENGINE_TARGET]
    if len(assets) != 1:
        sys.exit(f"Drupack {version} lists {len(assets)} {ENGINE_TARGET} engines, not one")
    body = download(assets[0]["url"])
    if hashlib.sha256(body).hexdigest() != assets[0]["sha256"]:
        sys.exit(f"Drupack {version}: {assets[0]['url']} does not match its sha256")
    path = Path(directory) / "drupack"
    path.write_bytes(body)
    path.chmod(0o755)
    return path


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


@functools.cache
def pinned_engine():
    """The pinned engine, downloaded once per run into a directory removed at exit."""
    directory = tempfile.mkdtemp()
    atexit.register(shutil.rmtree, directory)
    return engine(directory)


# Composer options for a run nobody watches.
QUIET = ("--no-interaction", "--no-progress")


def composer_run(*arguments):
    """Runs the pinned engine's Composer, which resolves against the PHP it carries. Its stdout goes to
    stderr: the summary is the only stdout, and the release job commits it."""
    subprocess.run([pinned_engine(), "composer", *arguments], cwd=ROOT, check=True, stdout=sys.stderr)


def composer_update(*options):
    composer_run("update", *options, "--no-install", *QUIET, "--no-audit")


def drupatch_reroll(packages):
    """drupatch's re-roll of the patches on `packages`: the JSON report it prints on stdout."""
    return subprocess.run([pinned_engine(), "composer", "drupatch:reroll", "--format=json", "--no-interaction",
                           *[f"--package={name}" for name in packages]],
                          cwd=ROOT, stdout=subprocess.PIPE, text=True).stdout


def pin_pattern(name):
    """The line of `name`'s pin in composer.json, its value captured."""
    return rf'^\s*"{re.escape(name)}": "([^"]+)"'


def conflict_path(path):
    """Where drupatch writes a re-roll that leaves regions to decide."""
    return path.removesuffix(".patch") + ".conflict.patch"


def outcome(row, written, refused):
    """One patch's outcome from drupatch's report: `rerolled`, `dropped`, `kept`, or `held` with its reason."""
    path, result = row["source"], row.get("result", {})
    proposal = result.get("reroll", {})
    # drupatch drops the declaration of every patch its release holds.
    if row["verdict"] == "merged":
        return "dropped", None
    if written.get(path) == "clean" and proposal.get("verified") is True:
        return "rerolled", None
    # A refused overwrite names the file drupatch meant to write.
    for target in (path, conflict_path(path)):
        if target in refused:
            return "held", refused[target]
    if result.get("syntax_errors"):
        return "held", f"broken syntax in {result['syntax_errors'][0]}"
    if result.get("failure_mode"):
        return "held", result["failure_mode"]
    if result.get("fuzzy"):
        return "held", "refused by 2.x"
    if written.get(conflict_path(path)) == "conflicts":
        regions = proposal.get("conflicts", [])
        files = ", ".join(sorted({region.get("file", "") for region in regions}))
        return "held", f"the re-roll leaves open regions in {files} ({len(regions)})"
    if written.get(path) == "clean":
        return "held", "the service did not verify the re-roll"
    if row["verdict"] == "applies":
        return "kept", None
    return "held", result.get("error") or row["verdict"]


def judge(declared, report, after):
    """Each declared patch's outcome, by path. The report carries the service's answer: remote input."""
    written = {entry["path"]: entry["status"] for entry in report["written"]}
    refused = {entry["path"]: entry["reason"] for entry in report["refused"]}
    rows = {row["source"]: row for row in report["plan"]["patches"]}
    outcomes = {}
    for name, patches in declared.items():
        for path in patches.values():
            row = rows.get(path)
            if row is None or row["version"] != after[name]:
                sys.exit(f"drupatch judged no {path} on {name} {after[name]}")
            if row["verdict"] not in VERDICTS:
                sys.exit(f"{path}: drupatch answered {row['verdict']!r}, not a verdict")
            # drupatch points a declaration at the file it wrote when that file is not the declared one.
            if row.get("result", {}).get("reroll", {}).get("path", path) not in (path, conflict_path(path)):
                sys.exit(f"{path}: drupatch pointed its declaration at {row['result']['reroll']['path']}")
            outcomes[path] = outcome(row, written, refused)
        kinds = {outcomes[path][0] for path in patches.values()}
        # A hold puts the package back on a release that still needs the patch drupatch dropped.
        if {"dropped", "held"} <= kinds:
            sys.exit(f"{name}: drupatch dropped one patch and could not re-roll another")
    return outcomes


def coupled(held, bumped, needs_before, needs_after):
    """`held` and every package bumped this run that goes back with it: one a held package's old release
    requires, or one whose new release requires a held package, until no more join."""
    group = set(held)
    while more := {name for name in bumped - group
                   if needs_after[name] & group or any(name in needs_before[member] for member in group)}:
        group |= more
    return group


def hold(summary, name, pin, patches, reasons, after):
    """Puts `name` back on `pin` and notes each reason that holds it there."""
    rewrite(COMPOSER, pin_pattern(name), pin)
    summary.rows = [row for row in summary.rows if row[0] != name]
    for path in patches.values():
        (ROOT / conflict_path(path)).unlink(missing_ok=True)
    summary.notes += [f"PATCH {name} {after[name]}: {reason}" for reason in reasons]


def reroll(summary, declared, pins, bumped, before, after, needs_before, needs_after):
    """Re-rolls the patches `declared` for each package whose locked version moved. A clean, verified
    re-roll replaces its patch file, and a patch the release holds is deleted. Any other answer puts the
    package back on its pin in `pins`, with every package bumped this run that depends on it either way."""
    originals = {path: (ROOT / path).read_bytes() for patches in declared.values() for path in patches.values()}
    composer_run("install", "--no-plugins", "--no-scripts", *QUIET)
    # drupatch prints its report once the service answered, and nothing on stdout when it did not.
    try:
        report = json.loads(drupatch_reroll(declared))
    except json.JSONDecodeError:
        sys.exit("drupatch printed no report")
    outcomes = judge(declared, report, after)
    failed = {name for name, patches in declared.items() if any(outcomes[path][0] == "held" for path in patches.values())}
    held = coupled(failed, bumped, needs_before, needs_after)
    for name in held - failed:
        if any(outcomes[path][0] == "dropped" for path in declared.get(name, {}).values()):
            sys.exit(f"{name}: drupatch dropped its patch, and the hold of {', '.join(sorted(failed))} keeps it back")
    done = set()
    for name, patches in declared.items():
        for path in patches.values():
            kind = outcomes[path][0]
            if kind == "dropped":
                (ROOT / path).unlink()
            elif kind != "rerolled" or name in held:
                (ROOT / path).write_bytes(originals[path])
                continue
            summary.row(f"{name} {path}", before[name], after[name], kind)
            done.add(kind)
    for name in sorted(held):
        patches = declared.get(name, {})
        reasons = [f"{path}: {outcomes[path][1]}" for path in patches.values() if outcomes[path][0] == "held"]
        hold(summary, name, pins[name], patches, reasons or [f"held with {', '.join(sorted(failed))}"], after)
    # The drop edits composer.json, so the lock's content hash moves; a hold's full update refreshes it too.
    if held:
        composer_update()
    elif "dropped" in done:
        composer_update("--lock")
    if done:
        composer_run("patches-relock")


def locked_versions():
    lock = json.loads((ROOT / "composer.lock").read_text())
    return {package["name"]: package["version"] for package in lock["packages"] + lock["packages-dev"]}


def locked_requires():
    """The packages each locked package requires, by name."""
    lock = json.loads((ROOT / "composer.lock").read_text())
    return {package["name"]: set(package.get("require", {})) for package in lock["packages"] + lock["packages-dev"]}


def composer(summary):
    manifest = json.loads((ROOT / COMPOSER).read_text())
    pins = {**manifest["require"], **manifest["require-dev"]}
    for name in manifest["extra"]["patches"]:
        if release_key(pins[name]) is None:
            sys.exit(f"{name} carries a patch, so a hold needs an exact pin, not {pins[name]}")
    bumped = set()
    for name, constraint in pins.items():
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
        rewrite(COMPOSER, pin_pattern(name), newest)
        summary.row(name, constraint, newest, "bumped")
        bumped.add(name)
    before, needs_before = locked_versions(), locked_requires()
    composer_update()
    solved = locked_versions()
    moved = {name: patches for name, patches in manifest["extra"]["patches"].items() if before[name] != solved[name]}
    if moved:
        reroll(summary, moved, pins, bumped, before, solved, needs_before, locked_requires())
    after = locked_versions()
    listed = {row[0] for row in summary.rows}
    for name in sorted(after):
        if before.get(name) != after[name] and name not in listed:
            summary.row(name, before.get(name, "none"), after[name], "locked")


def main():
    summary = Summary()
    drupack(summary)
    node(summary)
    composer(summary)
    sys.stdout.write(summary.render())


if __name__ == "__main__":
    main()
