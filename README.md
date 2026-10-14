# Drupacked Demo

Drupacked Demo is Drupal CMS on the Mercury Demo recipe, with WordPal and Agent
Access, packed as one executable by [Drupack](https://github.com/tresbientech/drupack).

This repository is a complete site repository. It builds, tests and releases
the site through Drupack's reusable GitHub workflow, and bumps its own pins.

## Install

The install script puts the latest release into the current directory:

```sh
curl -fsSL https://github.com/tresbientech/drupacked-demo/releases/latest/download/install-drupacked-demo.sh | sh
./drupacked-demo
```

On Windows, in PowerShell:

```powershell
irm https://github.com/tresbientech/drupacked-demo/releases/latest/download/install-drupacked-demo.ps1 | iex
.\drupacked-demo.exe
```

A release holds these builds:

- Linux amd64 and arm64, each for glibc and musl
- macOS arm64 and amd64
- Windows amd64

[Drupack's README](https://github.com/tresbientech/drupack#readme) covers what
the executable does once it runs.

## What the repository holds

- `composer.json` and `composer.lock`, the site's Composer project
- `drupack.yml`, the site contract
- `recipes/drupacked_demo_site`, the recipe a first start installs
- `agent-access.php`, the settings the generated `settings.php` requires last
- `patches/`, applied by `cweagans/composer-patches`
- `tests/`, conformance cases the build runs after Drupack's own
- `.github/workflows/build.yml`, which calls Drupack's `build.yml` at a Drupack tag
- `build/upstream.py`, the bumper, with its tests beside it
- `.gitea/workflows/`, the release job and the mirror

## Build

A tag push on GitHub builds the executables, runs the conformance suite on
them and publishes a release. A dispatch of the same workflow builds and tests
a branch, and publishes nothing.

A local build is one command in Drupack's job image, with `VERSION` the Drupack tag
`.github/workflows/build.yml` names. It packs the Linux targets alone, so
`--platform` names them:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/site" -w /site \
    ghcr.io/tresbientech/drupack-build:VERSION \
    drupack-build --site . --platform linux-amd64 --output dist
```

## Release

The repository is canonical on a Gitea forge and mirrored to GitHub.
`.gitea/workflows/upstream.yml` cuts every release there, each day at 09:17 UTC
and on dispatch:

1. `build/upstream.py` moves each pin to its newest version inside its line.
2. The job commits the bump to a candidate branch and pushes it to GitHub.
3. GitHub builds and tests the candidate.
4. On a green run the job pushes `main` and a version tag, and the mirror
   carries both to GitHub, where the tag run publishes.

A dispatch with `release` skips the bump and releases `main` as it is.
A dispatch with `dry_run` prints the bump and pushes nothing.

A version is `YEAR.MONTH.N`, such as `2026.10.1`. `N` counts the month's
releases from 1.

## Pins

`build/upstream.py` bumps:

- the Drupack tag in `.github/workflows/build.yml`, to Drupack's latest release
- `config.platform.php` in `composer.json`, to the PHP that Drupack release runs
- the Node release in `drupack.yml`, inside its major line
- each exact Composer pin, inside its major line and at its stability or better
- the lock, with `composer update`

It reports a new major and leaves it alone. `HOLDS` in the script names each
package kept at its pin, with the breakage a newer release brings.

```sh
python3 build/upstream.py              # bump the working tree, print the summary
python3 -m unittest discover -s build  # the bumper's tests
```

A site on GitHub alone can use Renovate in place of this script.
[Drupack's guide](https://github.com/tresbientech/drupack/blob/main/docs/build-your-site.md#keeping-the-pins-current)
gives its configuration.

## WordPal's patches

The demo carries WordPal's Display Builder target, not installed. `patches/`
holds the patches WordPal's usage guide names, copied from WordPal's `patches`
directory:

- four for Display Builder and UI Patterns, which the Display Builder target needs
- one for Canvas, which shows each layer's label in the Layers panel

composer-patches applies them, and `patches.lock.json` records each one's
SHA-256. Patches that dependencies declare stay unapplied.

When WordPal changes its patches, copy them again and run
`composer patches-relock`.

## License

GPL-2.0-or-later, as Drupal.
