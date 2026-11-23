# Plan: re-roll broken patches in the bumper with drupatch

**Spec:** docs/prd/drupatch-reroll.md

## Architectural decisions

Durable decisions that apply across all phases, in the PRD's words:

- **D1**: The bumper re-rolls patches after its `composer update`, with `drupatch:reroll --format json` run through the pinned engine's Composer.
- **D2**: drupatch runs only when this run changed the locked version of a package that carries a patch.
- **D3**: Before drupatch runs, the bumper runs `composer install --no-plugins --no-scripts`, which fills `vendor/` and applies no patch.
- **D4**: A re-roll that merges cleanly replaces the patch file, and the candidate commits it.
- **D5**: Open regions, broken syntax, `refused by 2.x` or `unknown` put the package back on its previous pin, with each package bumped in the run that depends on it either way, restore the patch files, and add a `PATCH` note.
- **D6**: A `merged` verdict drops the declaration and the patch file, then refreshes the lock with `composer update --lock`, or with the hold's full update. A drop and a hold on one package stop the run.
- **D7**: After any re-roll or drop, the bumper runs `composer patches-relock`.
- **D8**: The bumper reads drupatch's JSON report alone. No report, an unknown verdict string, or a repointed declaration stops the bumper with a non-zero exit.
- **D9**: `tresbientech/drupal-patch-check` sits in `require-dev` at an exact pin, allowed as a plugin, with `patch-directory` set to `patches`.
- **D10**: `composer()` bumps exact pins in `require-dev` the same way as pins in `require`.
- **D11**: The summary gets a `rerolled` or `dropped` row per patch, and a `PATCH` note per hold.
- **D12**: The README describes drupatch, the re-roll step and the hold, and fixes the patch count.
- **drupatch boundary**: the JSON document on stdout is remote data. The bumper reads `written`, `refused` and each `plan.patches` row (`source`, `version`, `verdict`, `result`), and validates what it reads.
- **Verdict strings**: `applies`, `merged`, `conflicts`, `unknown`. Broken syntax and `refused by 2.x` come from a row's `result.syntax_errors` and `result.fuzzy`.

---

## Phase 1: drupatch in the site's dev dependencies

**User stories**: 6, 7
**Decisions**: D9, D10, D12

### What to build

Require drupatch in `require-dev` at its newest release, allow the plugin, and set `patch-directory`. Lock it with the pinned engine's Composer. Extend the bumper so a `require-dev` exact pin moves like a `require` pin. The README's "What the repository holds" names drupatch, and its patch count reads two for Display Builder and UI Patterns.

### Acceptance criteria

- [ ] `python3 -m unittest discover -s build` passes, with a new test where a `require-dev` exact pin with a newer release in its line is rewritten and gets a `bumped` row.
- [ ] `jq '.["require-dev"]["tresbientech/drupal-patch-check"], .config["allow-plugins"]["tresbientech/drupal-patch-check"], .extra["drupal-patch-check"]' composer.json` prints an exact version, `true` and `{"patch-directory": "patches"}`.
- [ ] `jq -r '.["packages-dev"][].name' composer.lock` lists `tresbientech/drupal-patch-check`, and `drupack composer validate --no-check-publish` reports the lock in sync.
- [ ] `grep -c drupal-patch-check README.md` is at least 1, and `grep -n 'four for Display Builder' README.md` prints nothing.

---

## Phase 2: clean re-roll end to end

**User stories**: 1, 5
**Decisions**: D1, D2, D3, D4, D7, D8, D11, D12

### What to build

After the lock update, the bumper compares locked versions before and after. When a package with a declared patch moved, it installs the locked tree with plugins and scripts off, then runs drupatch's re-roll in JSON. A clean re-roll stays in the tree, relock records its hash, and the summary gets a `rerolled` row. No report on stdout or an unknown verdict string stops the bumper. The README's "Pins" section adds the re-roll step, and "WordPal's patches" says the bumper re-rolls the copies.

### Acceptance criteria

- [ ] `python3 -m unittest discover -s build` passes, with new tests for each case:
  - no patched package moved: no install, no drupatch call, no relock
  - clean re-roll: the patch file holds the new text, relock ran, a `rerolled` row names the package and file
  - drupatch prints no report: the bumper exits non-zero
  - an unknown verdict string: the bumper exits non-zero
- [ ] Live run in a scratch copy of the worktree, outside the repo: the canvas patch from `870b7ca^` and canvas pinned at its release before 1.13.0. The bumper exits 0, and `git diff --stat` shows `patches/canvas-layer-node-name.patch` and `patches.lock.json` changed. The phase report pastes the summary.
- [ ] `grep -n 'patches-relock' README.md` names the bumper's re-roll step, and `grep -n 'copy them again' README.md` prints nothing.

---

## Phase 3: hold a package whose patch will not re-roll cleanly

**User stories**: 2, 4
**Decisions**: D5, D11, D12

### What to build

A patch that comes back with open regions, broken syntax, `refused by 2.x` or `unknown` puts its package back on the previous pin. The bumper restores the patch file, deletes the `.conflict.patch`, and runs the lock update again. The summary gets a `PATCH` note with the package, the refused release and the reason. A patched package that is not an exact pin stops the run. The README's "Pins" section describes the hold.

### Acceptance criteria

- [ ] `python3 -m unittest discover -s build` passes, with new tests for each case:
  - open regions: the pin is back on its old version, the patch file matches `HEAD`, no `*.conflict.patch` exists, the second lock update ran, a `PATCH` note names the reason
  - `broken syntax`, `refused by 2.x` and `unknown`: each gives the same hold
  - a hold and a clean re-roll in one run: one `PATCH` note and one `rerolled` row
  - a patched package declared with a range: the bumper exits non-zero
- [ ] `grep -n 'PATCH' README.md` describes the hold.

---

## Phase 4: drop a patch the release already holds

**User stories**: 3
**Decisions**: D6, D11

### What to build

A `merged` verdict leaves drupatch's removal of the declaration in `composer.json`. The bumper deletes the patch file, refreshes the lock hash with `composer update --lock`, runs relock, and adds a `dropped` row.

### Acceptance criteria

- [ ] `python3 -m unittest discover -s build` passes, with new tests:
  - `merged`: the declaration is absent from `composer.json`, the patch file is gone, the lock refresh and relock ran, a `dropped` row names the package
  - `merged` beside a hold in one run: the dropped package is not reverted
