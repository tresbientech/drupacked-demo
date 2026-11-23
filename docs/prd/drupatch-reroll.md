# PRD: re-roll broken patches in the bumper with drupatch

## Problem Statement

`build/upstream.py` moves every exact pin to its newest release each day. It never looks at the three patches in `patches/`. When a release breaks a patch, `composer install` fails in the GitHub build, and every candidate fails until someone rebuilds the patch by hand.

canvas 1.13.0 rebuilt its minified bundle and broke `canvas-layer-node-name.patch`. Candidates failed from 2026-11-19 to 2026-11-22, and no release shipped, until commit 870b7ca rebuilt the patch.

## Solution

The bumper runs drupatch (`tresbientech/drupal-patch-check`) after it moves the pins. drupatch asks `api.tresbien.tech` about each patch on a package whose locked version moved.

- A patch drupatch re-rolls cleanly is rewritten, and the candidate carries it.
- A patch the release already holds is dropped.
- A patch drupatch cannot re-roll cleanly keeps its package at the old pin, and the other bumps still ship.
- The summary, which becomes the candidate's commit message, lists each case.

## Out of Scope

- Carrying re-rolled patches back to WordPal's `patches/` directory. The demo's canvas patch already differs from WordPal's.
- Running `drupatch:check` in GitHub `build.yml` on branches.
- drupatch's shipped detection: `merged` fires only when the fix is in the release verbatim, so a fix that shipped refactored reads `applies` or `conflicts`.
- A Renovate setup.
- Changes to drupatch itself.

## User Stories

1. As the site maintainer, I want the bumper to re-roll a patch a new release broke, so that releases keep shipping without a hand rebuild.
2. As the site maintainer, I want a package whose patch cannot be re-rolled cleanly held at its old pin, so that the other bumps still ship.
3. As the site maintainer, I want a patch the release already holds removed, so that the site stops carrying it.
4. As the site maintainer, I want each re-roll, drop and hold listed in the candidate's commit message, so that I can review what the bumper did to a patch.
5. As the site maintainer, I want the bumper to stop when drupatch cannot reach its service, so that no candidate ships with patches nobody checked.
6. As the site maintainer, I want drupatch installed in the site's dev dependencies, so that I can run its check and re-roll by hand in my checkout.
7. As the site maintainer, I want the bumper to move drupatch's own pin, so that it stays current like every other pin.

## Implementation Decisions

D1. The bumper re-rolls patches after its `composer update`, with `drupatch:reroll --format json` run through the pinned engine's Composer. Evidence: `composer_update()` and `engine()` in `build/upstream.py`.

D2. drupatch runs only when this run changed the locked version of a package that carries a patch. With no such change, the bumper makes no call to the service. Evidence: `locked_versions()` in `build/upstream.py`; the keys of `extra.patches` in `composer.json`.

D3. Before drupatch runs, the bumper runs `composer install --no-plugins --no-scripts`. This fills `vendor/` with drupatch and applies no patch. Composer loads installed plugins on its next call, and composer-patches applies patches only on install and update events. Disabling every patcher instead throws "No patchers available" (`cweagans/composer-patches` `src/Patcher.php:113`). drupatch reads `composer.lock`, not `vendor/` (`src/Read/Site.php:189`).

D4. A re-roll that merges cleanly replaces the patch file, and the candidate commits it. drupatch reports each file it wrote, with its path and status (`src/Render/Report.php:603`).

D5. A patch comes back with open regions, broken syntax, `refused by 2.x` or `unknown`. The bumper then restores the patch file and deletes any `.conflict.patch` beside it. It puts the package back on its previous pin, with every package bumped in the same run that requires it or that its old release requires. Then it runs `composer update` again. The summary gets a `PATCH` note naming the package, the release it refused and the reason.
- Invariant: every patched package is an exact pin in `require`: canvas `1.13.0`, display_builder `1.0.0-beta10`, ui_patterns `2.0.23`.
- A patched package without an exact pin stops the run with an error, before the first update.
- A coupled package whose patch drupatch dropped stops the run before any write.

D6. A `merged` verdict: drupatch drops the declaration from `composer.json`, and the bumper deletes the patch file. That edit changes the content hash in `composer.lock`. With no hold in the run, the bumper refreshes the lock with `composer update --lock`. A hold's full update refreshes it too. A package with both a dropped and a held patch stops the run before any write.

D7. After any re-roll or drop, the bumper runs `composer patches-relock` to record the new hashes in `patches.lock.json`. drupatch runs relock only in `add` and `upgrade` (`src/ManagerCommands.php:35`).

D8. The bumper decides from drupatch's JSON report alone, never from its exit code or console text. drupatch prints no report when the service gave no answer. No report, a verdict the bumper does not know, or a declaration drupatch pointed at another file stops the bumper with a non-zero exit. The release job's bump step runs with `pipefail`, so that exit fails the step and no candidate is pushed.

D9. `tresbientech/drupal-patch-check` goes into `require-dev` at an exact pin, with `allow-plugins` set to true. `extra.drupal-patch-check.patch-directory` is `patches`, so `drupatch:add` and `drupatch:pin` write into the existing directory. `hook` stays unset, and its default is off. The executable never holds drupatch: Drupack builds with `composer install --no-dev` (portable-drupal `launcher/internal/build/build.go:117`).

D10. `composer()` bumps exact pins in `require-dev` the same way as pins in `require`. It reads `require` alone today (`build/upstream.py`, `composer()`).

D11. The summary gets one row per re-roll and per drop, with the action `rerolled` or `dropped`. A hold is a `PATCH` note (D5) beside the existing `HELD` and `MAJOR` notes. Evidence: `Summary` in `build/upstream.py`.

D12. The README changes:
- "WordPal's patches" says the patches started as copies of WordPal's and that the bumper re-rolls them. The "copy again and relock" instruction goes.
- "Pins" adds the re-roll step and the hold.
- "What the repository holds" names drupatch in `require-dev`.
- The patch count reads two for Display Builder and UI Patterns, not four.

### Considered options

- Check, then hold: run `drupatch:check` and hold every package whose patch conflicts. Lost because every broken patch would wait for a hand rebuild, which is the delay this change removes.
- Check and fail fast: stop the bumper on a conflict. Lost because no release ships until a person acts.
- Dev tool only: drupatch in `require-dev`, run by hand. Lost because CI stays blind to broken patches.
- Install drupatch at bump time as a global plugin in a throwaway `COMPOSER_HOME`. It skips the install of D3, but the bumper would pin drupatch's version itself, and a hand run would need a global install. The owner chose `require-dev`.

## Testing Decisions

A test feeds the bumper recorded drupatch output and asserts on the files the bumper leaves, the summary it prints, and the order of its Composer steps. The order is behaviour here: relock runs last (R1), and a hold runs a second update.

Tests go in `build/test_upstream.py`, which copies the real pin files into a scratch tree. They replace `upstream.fetch` and `upstream.composer_update` the same way the `Bumper` case does. The new subprocess calls get the same treatment.

Cases:

- no patched package moved: no drupatch call
- clean re-roll: patch file replaced, `rerolled` row, relock run
- open regions: patch file unchanged, no `.conflict.patch`, package back on its old pin, `PATCH` note
- `merged`: declaration and file gone, lock refreshed, `dropped` row
- drupatch prints no report: the bumper exits non-zero
- a `require-dev` exact pin bumped

## Further Notes

The 09:17 UTC upstream job checks out `main` and runs the bumper it finds there. Once this merges into `main`, the next scheduled run uses it.

A re-rolled patch reaches a release with no human review. The GitHub build and its conformance suite are the only checks on it. No conformance case covers the canvas layer label.
