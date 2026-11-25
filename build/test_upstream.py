"""build/upstream.py against copies of the real pin files and recorded indexes, and
build/next-version.py. Run with:

    python3 -m unittest discover -s build
"""

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "build" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


upstream = build_script("upstream")
LOCK = "composer.lock"
PINNED = [upstream.WORKFLOW, upstream.COMPOSER, LOCK, upstream.SITE]


class Bumper(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.tree = Path(scratch.name)
        for path in PINNED:
            (self.tree / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / path, self.tree / path)
        self.addCleanup(setattr, upstream, "ROOT", upstream.ROOT)
        self.addCleanup(setattr, upstream, "fetch", upstream.fetch)
        self.addCleanup(setattr, upstream, "composer_update", upstream.composer_update)
        upstream.ROOT = self.tree
        self.indexes = {}
        upstream.fetch = self.fetch

    def fetch(self, url):
        if url not in self.indexes:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.indexes[url]

    def run_bumper(self, source):
        summary = upstream.Summary()
        source(summary)
        return summary

    def text(self, path):
        return (self.tree / path).read_text()

    def pin(self, name, section="require"):
        return json.loads(self.text(upstream.COMPOSER))[section][name]

    def serve_drupack(self, version):
        self.indexes[upstream.DRUPACK_RELEASE] = json.dumps({"version": version})

    def serve_node(self, *releases):
        self.indexes[upstream.NODE_RELEASES] = json.dumps(
            [{"version": f"v{version}", "lts": lts} for version, lts in releases])

    def serve_composer(self, **listed):
        """Each require entry's metadata: the versions `listed` names, else its pin alone."""
        manifest = json.loads(self.text(upstream.COMPOSER))
        for name, constraint in [*manifest["require"].items(), *manifest["require-dev"].items()]:
            base = upstream.DRUPAL_METADATA if name.startswith("drupal/") else upstream.PACKAGIST_METADATA
            versions = listed.get(name, [constraint])
            self.indexes[f"{base}{name}.json"] = json.dumps({"packages": {name: [{"version": v} for v in versions]}})
        self.updated = False
        upstream.composer_update = lambda: setattr(self, "updated", True)

    def test_a_newer_drupack_release_rewrites_the_workflow_ref(self):
        current = upstream.read(upstream.WORKFLOW, upstream.DRUPACK_PIN)
        self.indexes[upstream.DRUPACK_RELEASE] = json.dumps({"version": "9.0.0"})
        summary = self.run_bumper(upstream.drupack)
        self.assertEqual(summary.rows, [("Drupack", current, "9.0.0", "bumped")])
        self.assertIn("build.yml@9.0.0\n", self.text(upstream.WORKFLOW))

    def test_a_prerelease_number_past_nine_counts_as_newer(self):
        workflow = self.tree / upstream.WORKFLOW
        current = upstream.read(upstream.WORKFLOW, upstream.DRUPACK_PIN)
        workflow.write_text(workflow.read_text().replace(current, "1.0.0-alpha9"))
        self.serve_drupack("1.0.0-alpha10")
        self.run_bumper(upstream.drupack)
        self.assertIn("build.yml@1.0.0-alpha10\n", self.text(upstream.WORKFLOW))

    def test_the_pinned_drupack_release_writes_nothing(self):
        current = upstream.read(upstream.WORKFLOW, upstream.DRUPACK_PIN)
        self.indexes[upstream.DRUPACK_RELEASE] = json.dumps({"version": current})
        before = {path: self.text(path) for path in PINNED}
        summary = self.run_bumper(upstream.drupack)
        self.assertEqual(summary.rows, [("Drupack", current, current, "current")])
        self.assertEqual({path: self.text(path) for path in PINNED}, before)

    def test_a_latest_release_that_names_no_version_stops_the_run(self):
        self.indexes[upstream.DRUPACK_RELEASE] = json.dumps({"version": "main; rm -rf"})
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.drupack)

    def serve_engine(self, body, sha256):
        version = upstream.read(upstream.WORKFLOW, upstream.DRUPACK_PIN)
        url = f"{upstream.DRUPACK_DOWNLOADS}{version}/drupack-{version}-linux-amd64"
        self.indexes[f"{upstream.DRUPACK_DOWNLOADS}{version}/release.json"] = json.dumps({"assets": [
            {"name": "drupack", "target": "linux-amd64-musl", "url": url + "-musl", "sha256": "0" * 64},
            {"name": "drupack", "target": "linux-amd64", "url": url, "sha256": sha256}]})
        self.addCleanup(setattr, upstream, "download", upstream.download)
        upstream.download = lambda requested: body if requested == url else self.fail(requested)

    def test_the_engine_is_the_pinned_release_for_the_runner(self):
        self.serve_engine(b"engine", hashlib.sha256(b"engine").hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            path = upstream.engine(directory)
            self.assertEqual(path.read_bytes(), b"engine")
            self.assertTrue(os.access(path, os.X_OK))

    def test_an_engine_that_misses_its_sha256_stops_the_run_before_any_write(self):
        self.serve_engine(b"tampered", hashlib.sha256(b"engine").hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                upstream.engine(directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_newer_node_lts_rewrites_the_pin(self):
        current = upstream.read(upstream.SITE, upstream.NODE_PIN)
        line = current.split(".")[0]
        self.serve_node((f"{int(line) + 1}.9.0", False), (f"{line}.99.1", "Krypton"), (current, "Krypton"))
        summary = self.run_bumper(upstream.node)
        self.assertEqual(summary.rows, [("Node", current, f"{line}.99.1", "bumped")])
        self.assertIn(f"\nnode: {line}.99.1\n", self.text(upstream.SITE))

    def test_a_newer_node_lts_line_is_reported_and_not_taken(self):
        current = upstream.read(upstream.SITE, upstream.NODE_PIN)
        line = current.split(".")[0]
        above = f"{int(line) + 2}.1.0"
        self.serve_node((above, "Next"), (current, "Krypton"))
        summary = self.run_bumper(upstream.node)
        self.assertEqual(summary.rows, [("Node", current, current, "current")])
        self.assertEqual(summary.notes, [f"MAJOR Node {above} LTS is out, outside the {line} line"])

    def test_an_alpha_moves_to_a_newer_beta(self):
        current = self.pin("drupal/wordpal")
        self.serve_composer(**{"drupal/wordpal": ["1.0.0-beta1", current]})
        summary = self.run_bumper(upstream.composer)
        self.assertIn(("drupal/wordpal", current, "1.0.0-beta1", "bumped"), summary.rows)
        self.assertEqual(self.pin("drupal/wordpal"), "1.0.0-beta1")
        self.assertTrue(self.updated)

    def test_a_stable_pin_never_moves_to_a_prerelease(self):
        current = self.pin("drupal/canvas")
        self.serve_composer(**{"drupal/canvas": ["1.99.0-beta1", current]})
        summary = self.run_bumper(upstream.composer)
        self.assertIn(("drupal/canvas", current, current, "current"), summary.rows)
        self.assertEqual(self.pin("drupal/canvas"), current)

    def test_a_new_major_is_reported_and_not_taken(self):
        current = self.pin("drupal/canvas")
        self.serve_composer(**{"drupal/canvas": ["2.0.0", "1.99.1", current]})
        summary = self.run_bumper(upstream.composer)
        self.assertEqual(self.pin("drupal/canvas"), "1.99.1")
        self.assertIn("MAJOR drupal/canvas 2.0.0 is out, outside the 1.x line", summary.notes)

    def test_a_dev_pin_moves_like_a_require_pin(self):
        name = "tresbientech/drupal-patch-check"
        current = self.pin(name, "require-dev")
        self.serve_composer(**{name: ["v0.99.0", f"v{current}"]})
        summary = self.run_bumper(upstream.composer)
        self.assertIn((name, current, "0.99.0", "bumped"), summary.rows)
        self.assertEqual(self.pin(name, "require-dev"), "0.99.0")

    def test_a_held_package_is_reported_with_its_reason_and_left_alone(self):
        name = "cweagans/composer-patches"
        current = self.pin(name)
        self.addCleanup(setattr, upstream, "HOLDS", upstream.HOLDS)
        upstream.HOLDS = {name: "cron fails"}
        self.serve_composer(**{name: ["3.0.0-alpha1", "2.99.0", current]})
        summary = self.run_bumper(upstream.composer)
        self.assertEqual(self.pin(name), current)
        self.assertIn(f"HELD {name} {current} (2.99.0): cron fails", summary.notes)
        self.assertNotIn(name, [row[0] for row in summary.rows])

    def test_composer_update_moves_the_lock_and_each_move_is_listed(self):
        self.serve_composer()
        lock = self.tree / LOCK

        def update():
            content = json.loads(lock.read_text())
            core = next(package for package in content["packages"] if package["name"] == "drupal/core")
            core["version"] = "11.99.0"
            lock.write_text(json.dumps(content))
        upstream.composer_update = update
        summary = self.run_bumper(upstream.composer)
        self.assertEqual([row[0] for row in summary.rows if row[3] == "locked"], ["drupal/core"])
        self.assertEqual(next(row for row in summary.rows if row[0] == "drupal/core")[2], "11.99.0")

    def test_a_drupal_package_absent_from_drupal_org_comes_from_packagist(self):
        self.serve_composer()
        name = "drupal/mercury_demo"
        current = self.pin(name)
        del self.indexes[f"{upstream.DRUPAL_METADATA}{name}.json"]
        self.indexes[f"{upstream.PACKAGIST_METADATA}{name}.json"] = json.dumps(
            {"packages": {name: [{"version": "1.99.1"}, {"version": current}]}})
        summary = self.run_bumper(upstream.composer)
        self.assertIn((name, current, "1.99.1", "bumped"), summary.rows)

    CANVAS_PATCH = "patches/canvas-layer-node-name.patch"
    UI_PATTERNS_PATCH = "patches/ui_patterns-zero-prop-value.patch"

    def move_lock(self, *moves):
        """Serves each (name, version) as the newest release of its line, and has composer update lock each
        package at its pin, or at `version` for one composer.json does not pin exactly."""
        pins = self.required()
        self.serve_composer(**{name: [version, pins[name]] for name, version in moves
                               if upstream.release_key(pins.get(name, "")) is not None})
        lock = self.tree / LOCK
        self.calls = []

        def update(*options):
            self.calls.append(("update", *options))
            content = json.loads(lock.read_text())
            pins = self.required()
            for name, version in moves:
                pin = pins.get(name, "")
                package = next(package for package in content["packages"] if package["name"] == name)
                package["version"] = pin if upstream.release_key(pin) is not None else version
            lock.write_text(json.dumps(content))
        upstream.composer_update = update

    def required(self):
        return json.loads(self.text(upstream.COMPOSER))["require"]

    def steps(self):
        """Each Composer call by its command, `update --lock` told apart from a full update."""
        return [" ".join(call[:2]) if call[1:2] == ("--lock",) else call[0] for call in self.calls]

    def serve_reroll(self, rows, written=(), refused=(), writes=None, drops=(), report=True):
        """drupatch answers `rows`, writing each of `writes` (path: text) and dropping the declarations of the
        packages in `drops` as it would. Without a `report` it prints nothing."""
        shutil.copytree(ROOT / "patches", self.tree / "patches")
        self.originals = {path.name: path.read_bytes() for path in (self.tree / "patches").iterdir()}
        self.addCleanup(setattr, upstream, "composer_run", upstream.composer_run)
        self.addCleanup(setattr, upstream, "drupatch_reroll", upstream.drupatch_reroll)
        upstream.composer_run = lambda *arguments: self.calls.append(arguments)

        def drupatch(packages):
            self.calls.append(("drupatch:reroll",))
            for path, text in (writes or {}).items():
                (self.tree / path).write_text(text)
            composer = self.tree / upstream.COMPOSER
            manifest = json.loads(composer.read_text())
            for name in drops:
                del manifest["extra"]["patches"][name]
            composer.write_text(json.dumps(manifest, indent=4))
            if not report:
                return ""
            return json.dumps({"plan": {"patches": rows},
                               "written": [{"path": path, "status": status} for path, status in written],
                               "refused": [{"path": path, "reason": reason} for path, reason in refused]})
        upstream.drupatch_reroll = drupatch

    def patch_row(self, verdict, source=None, package="drupal/canvas", version="1.99.0", **result):
        return {"package": package, "source": source or self.CANVAS_PATCH, "version": version, "verdict": verdict,
                "result": result}

    def clean(self):
        return {"reroll": {"status": "clean", "verified": True}}

    def assert_untouched(self, path):
        self.assertEqual((self.tree / path).read_bytes(), self.originals[Path(path).name])

    def test_no_patched_package_moved_calls_no_drupatch(self):
        self.move_lock(("drupal/core", "11.99.0"))
        self.serve_reroll([])
        summary = self.run_bumper(upstream.composer)
        self.assertEqual(self.steps(), ["update"])
        self.assertNotIn("rerolled", [row[3] for row in summary.rows])

    def test_a_clean_reroll_replaces_the_patch_and_relocks(self):
        current = self.pin("drupal/canvas")
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("conflicts", **self.clean())],
                          written=[(self.CANVAS_PATCH, "clean")], writes={self.CANVAS_PATCH: "rerolled\n"})
        summary = self.run_bumper(upstream.composer)
        self.assertEqual(self.text(self.CANVAS_PATCH), "rerolled\n")
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll", "patches-relock"])
        self.assertIn((f"drupal/canvas {self.CANVAS_PATCH}", current, "1.99.0", "rerolled"), summary.rows)
        self.assertEqual(self.pin("drupal/canvas"), "1.99.0")

    def test_a_patch_that_still_applies_is_kept_without_a_relock(self):
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("applies")])
        summary = self.run_bumper(upstream.composer)
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll"])
        self.assertNotIn("rerolled", [row[3] for row in summary.rows])
        self.assertEqual(self.pin("drupal/canvas"), "1.99.0")

    def assert_held(self, row, reason, written=(), refused=(), writes=None):
        current = self.pin("drupal/canvas")
        locked = upstream.locked_versions()["drupal/canvas"]
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([row], written=written, refused=refused, writes=writes)
        summary = self.run_bumper(upstream.composer)
        self.assertEqual(self.pin("drupal/canvas"), current)
        self.assertEqual(upstream.locked_versions()["drupal/canvas"], locked)
        self.assert_untouched(self.CANVAS_PATCH)
        self.assertEqual(list((self.tree / "patches").glob("*.conflict.patch")), [])
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll", "update"])
        self.assertNotIn("drupal/canvas", [row[0] for row in summary.rows])
        self.assertIn(f"PATCH drupal/canvas 1.99.0: {self.CANVAS_PATCH}: {reason}", summary.notes)

    def test_a_reroll_with_open_regions_holds_the_package(self):
        conflict = upstream.conflict_path(self.CANVAS_PATCH)
        self.assert_held(self.patch_row("conflicts", reroll={"status": "conflicts", "verified": False, "path": conflict,
                                                            "conflicts": [{"file": "ui/dist/assets/index.js"}]}),
                         "the re-roll leaves open regions in ui/dist/assets/index.js (1)",
                         written=[(conflict, "conflicts")], writes={conflict: "<<<<<<<\n"})

    def test_broken_syntax_holds_the_package(self):
        self.assert_held(self.patch_row("conflicts", syntax_errors=["src/Layer.php: unexpected }"]),
                         "broken syntax in src/Layer.php: unexpected }")

    def test_a_patch_only_a_lenient_apply_takes_holds_the_package(self):
        self.assert_held(self.patch_row("applies", fuzzy=True), "refused by 2.x")

    def test_an_unknown_verdict_holds_the_package(self):
        self.assert_held(self.patch_row("unknown"), "unknown")

    def test_an_unknown_verdict_gives_the_service_error_as_its_reason(self):
        self.assert_held(self.patch_row("unknown", error="the release archive is missing"),
                         "the release archive is missing")

    DISPLAY_BUILDER_PATCH = "patches/display_builder-node-label.patch"

    def test_a_hold_takes_back_a_bumped_package_that_requires_it(self):
        display_builder = self.pin("drupal/display_builder")
        ui_patterns = self.pin("drupal/ui_patterns")
        self.move_lock(("drupal/display_builder", "1.0.0-beta99"), ("drupal/ui_patterns", "2.99.0"))
        self.serve_reroll([self.patch_row("unknown", self.UI_PATTERNS_PATCH, "drupal/ui_patterns", "2.99.0"),
                           self.patch_row("applies", self.DISPLAY_BUILDER_PATCH, "drupal/display_builder",
                                          "1.0.0-beta99")])
        summary = self.run_bumper(upstream.composer)
        self.assertEqual((self.pin("drupal/display_builder"), self.pin("drupal/ui_patterns")),
                         (display_builder, ui_patterns))
        self.assertIn("PATCH drupal/display_builder 1.0.0-beta99: held with drupal/ui_patterns", summary.notes)
        self.assertIn(f"PATCH drupal/ui_patterns 2.99.0: {self.UI_PATTERNS_PATCH}: unknown", summary.notes)
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll", "update"])

    def test_a_hold_takes_back_a_bumped_package_its_old_release_requires(self):
        display_builder = self.pin("drupal/display_builder")
        ui_patterns = self.pin("drupal/ui_patterns")
        self.move_lock(("drupal/display_builder", "1.0.0-beta99"), ("drupal/ui_patterns", "2.99.0"))
        self.serve_reroll([self.patch_row("unknown", self.DISPLAY_BUILDER_PATCH, "drupal/display_builder",
                                          "1.0.0-beta99"),
                           self.patch_row("conflicts", self.UI_PATTERNS_PATCH, "drupal/ui_patterns", "2.99.0",
                                          **self.clean())],
                          written=[(self.UI_PATTERNS_PATCH, "clean")], writes={self.UI_PATTERNS_PATCH: "rerolled\n"})
        summary = self.run_bumper(upstream.composer)
        self.assertEqual((self.pin("drupal/display_builder"), self.pin("drupal/ui_patterns")),
                         (display_builder, ui_patterns))
        self.assert_untouched(self.UI_PATTERNS_PATCH)
        self.assertNotIn("rerolled", [row[3] for row in summary.rows])
        self.assertIn("PATCH drupal/ui_patterns 2.99.0: held with drupal/display_builder", summary.notes)
        self.assertNotIn("patches-relock", self.steps())

    def test_a_hold_beside_the_drupack_and_node_rows_reads_composer_bumps_alone(self):
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("unknown")])
        summary = upstream.Summary()
        summary.row("Drupack", "1.0.0", "1.0.1", "bumped")
        summary.row("Node", "26.1.0", "26.2.0", "bumped")
        upstream.composer(summary)
        self.assertIn(f"PATCH drupal/canvas 1.99.0: {self.CANVAS_PATCH}: unknown", summary.notes)
        self.assertIn(("Drupack", "1.0.0", "1.0.1", "bumped"), summary.rows)

    def test_a_dropped_patch_on_a_package_a_hold_takes_back_stops_the_run(self):
        self.move_lock(("drupal/display_builder", "1.0.0-beta99"), ("drupal/ui_patterns", "2.99.0"))
        self.serve_reroll([self.patch_row("unknown", self.DISPLAY_BUILDER_PATCH, "drupal/display_builder",
                                          "1.0.0-beta99"),
                           self.patch_row("merged", self.UI_PATTERNS_PATCH, "drupal/ui_patterns", "2.99.0")],
                          drops=["drupal/ui_patterns"])
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll"])

    def test_a_clean_reroll_the_service_did_not_verify_holds_the_package(self):
        self.assert_held(self.patch_row("conflicts", reroll={"status": "clean", "verified": False}),
                         "the service did not verify the re-roll",
                         written=[(self.CANVAS_PATCH, "clean")], writes={self.CANVAS_PATCH: "unverified\n"})

    def test_a_write_drupatch_refused_holds_the_package(self):
        self.assert_held(self.patch_row("conflicts", **self.clean()), "the merged diff does not parse",
                         refused=[(self.CANVAS_PATCH, "the merged diff does not parse")])

    def test_a_hold_and_a_clean_reroll_in_one_run(self):
        ui_patterns = self.pin("drupal/ui_patterns")
        self.move_lock(("drupal/canvas", "1.99.0"), ("drupal/ui_patterns", "2.99.0"))
        self.serve_reroll([self.patch_row("unknown"),
                           self.patch_row("conflicts", self.UI_PATTERNS_PATCH, "drupal/ui_patterns", "2.99.0",
                                          **self.clean())],
                          written=[(self.UI_PATTERNS_PATCH, "clean")], writes={self.UI_PATTERNS_PATCH: "rerolled\n"})
        summary = self.run_bumper(upstream.composer)
        self.assertEqual([note for note in summary.notes if note.startswith("PATCH")],
                         [f"PATCH drupal/canvas 1.99.0: {self.CANVAS_PATCH}: unknown"])
        self.assertEqual([row for row in summary.rows if row[3] == "rerolled"],
                         [(f"drupal/ui_patterns {self.UI_PATTERNS_PATCH}", ui_patterns, "2.99.0", "rerolled")])
        self.assertEqual(self.text(self.UI_PATTERNS_PATCH), "rerolled\n")
        self.assert_untouched(self.CANVAS_PATCH)
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll", "update", "patches-relock"])

    def test_a_patch_the_release_holds_is_dropped(self):
        current = self.pin("drupal/canvas")
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("merged")], drops=["drupal/canvas"])
        summary = self.run_bumper(upstream.composer)
        self.assertNotIn("drupal/canvas", json.loads(self.text(upstream.COMPOSER))["extra"]["patches"])
        self.assertFalse((self.tree / self.CANVAS_PATCH).exists())
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll", "update --lock", "patches-relock"])
        self.assertIn((f"drupal/canvas {self.CANVAS_PATCH}", current, "1.99.0", "dropped"), summary.rows)
        self.assertEqual(self.pin("drupal/canvas"), "1.99.0")

    def test_a_drop_beside_a_hold_keeps_the_dropped_package_moved(self):
        canvas, ui_patterns = self.pin("drupal/canvas"), self.pin("drupal/ui_patterns")
        self.move_lock(("drupal/canvas", "1.99.0"), ("drupal/ui_patterns", "2.99.0"))
        self.serve_reroll([self.patch_row("unknown"),
                           self.patch_row("merged", self.UI_PATTERNS_PATCH, "drupal/ui_patterns", "2.99.0")],
                          drops=["drupal/ui_patterns"])
        summary = self.run_bumper(upstream.composer)
        self.assertEqual((self.pin("drupal/canvas"), self.pin("drupal/ui_patterns")), (canvas, "2.99.0"))
        locked = upstream.locked_versions()
        self.assertEqual((locked["drupal/canvas"], locked["drupal/ui_patterns"]), (canvas, "2.99.0"))
        self.assertFalse((self.tree / self.UI_PATTERNS_PATCH).exists())
        self.assert_untouched(self.CANVAS_PATCH)
        self.assertIn((f"drupal/ui_patterns {self.UI_PATTERNS_PATCH}", ui_patterns, "2.99.0", "dropped"), summary.rows)
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll", "update", "patches-relock"])

    def test_a_drop_and_a_hold_on_one_package_stop_the_run_before_any_write(self):
        composer = self.tree / upstream.COMPOSER
        manifest = json.loads(composer.read_text())
        manifest["extra"]["patches"]["drupal/canvas"]["Second"] = self.UI_PATTERNS_PATCH
        composer.write_text(json.dumps(manifest, indent=4))
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("merged"), self.patch_row("unknown", self.UI_PATTERNS_PATCH)],
                          writes={self.UI_PATTERNS_PATCH: "half\n"})
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)
        self.assertEqual(self.steps(), ["update", "install", "drupatch:reroll"])
        self.assertTrue((self.tree / self.CANVAS_PATCH).exists())

    def test_a_patched_package_without_an_exact_pin_stops_the_run(self):
        composer = self.tree / upstream.COMPOSER
        composer.write_text(composer.read_text().replace(f'"drupal/canvas": "{self.pin("drupal/canvas")}"',
                                                         '"drupal/canvas": "^1.13"'))
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("applies")])
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)

    def test_a_patched_package_without_an_exact_pin_stops_the_run_before_any_update(self):
        composer = self.tree / upstream.COMPOSER
        composer.write_text(composer.read_text().replace(f'"drupal/canvas": "{self.pin("drupal/canvas")}"',
                                                         '"drupal/canvas": "^1.13"'))
        self.move_lock(("drupal/core", "11.99.0"))
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)
        self.assertEqual(self.steps(), [])

    def test_drupatch_pointing_a_declaration_at_another_file_stops_the_run(self):
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("conflicts", reroll={"status": "clean", "verified": True,
                                                               "path": "patches/mr1.diff"})],
                          written=[("patches/mr1.diff", "clean")])
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)

    def test_drupatch_printing_no_report_stops_the_run(self):
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([], report=False)
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)

    def test_a_verdict_drupatch_does_not_give_stops_the_run(self):
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("probably")])
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)

    def test_a_moved_patch_drupatch_did_not_judge_stops_the_run(self):
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([])
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)

    def test_a_patch_judged_on_another_release_stops_the_run(self):
        self.move_lock(("drupal/canvas", "1.99.0"))
        self.serve_reroll([self.patch_row("applies", version="1.98.0")])
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)

    def test_a_package_no_repository_lists_stops_the_run(self):
        self.serve_composer()
        del self.indexes[f"{upstream.DRUPAL_METADATA}drupal/canvas.json"]
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)

    def test_a_pin_matching_nowhere_stops_the_run(self):
        (self.tree / upstream.WORKFLOW).write_text("jobs: {}\n")
        self.serve_drupack("9.0.0")
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.drupack)


class NextVersion(unittest.TestCase):
    def setUp(self):
        self.module = build_script("next-version")
        self.october = datetime(2026, 10, 13, tzinfo=timezone.utc)

    def test_a_month_without_a_release_starts_at_one(self):
        self.assertEqual(self.module.next_version(["2026.09.4", "0.1.5"], self.october), "2026.10.1")

    def test_a_release_raises_the_month_counter(self):
        self.assertEqual(self.module.next_version(["2026.10.1", "2026.10.2", "2026.09.7"], self.october), "2026.10.3")

    def test_a_counter_past_nine_counts_on(self):
        self.assertEqual(self.module.next_version(["2026.10.9", "2026.10.10"], self.october), "2026.10.11")

    def test_a_single_digit_month_keeps_two_digits(self):
        self.assertEqual(self.module.next_version([], datetime(2027, 1, 2, tzinfo=timezone.utc)), "2027.01.1")


if __name__ == "__main__":
    unittest.main()
