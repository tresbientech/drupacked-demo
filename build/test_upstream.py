"""build/upstream.py against copies of the real pin files and recorded indexes, and
build/next-version.py. Run with:

    python3 -m unittest discover -s build
"""

import importlib.util
import json
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

    def pin(self, name):
        return json.loads(self.text(upstream.COMPOSER))["require"][name]

    def serve_drupack(self, version, php):
        self.indexes[upstream.DRUPACK_RELEASE] = json.dumps({"version": version})
        self.indexes[f"{upstream.DRUPACK_SOURCE}{version}/runtime/builder-inputs.sh"] = php

    def serve_node(self, *releases):
        self.indexes[upstream.NODE_RELEASES] = json.dumps(
            [{"version": f"v{version}", "lts": lts} for version, lts in releases])

    def serve_composer(self, **listed):
        """Each require entry's metadata: the versions `listed` names, else its pin alone."""
        require = json.loads(self.text(upstream.COMPOSER))["require"]
        for name, constraint in require.items():
            base = upstream.DRUPAL_METADATA if name.startswith("drupal/") else upstream.PACKAGIST_METADATA
            versions = listed.get(name, [constraint])
            self.indexes[f"{base}{name}.json"] = json.dumps({"packages": {name: [{"version": v} for v in versions]}})
        self.updated = False
        upstream.composer_update = lambda: setattr(self, "updated", True)

    def test_a_newer_drupack_release_rewrites_the_workflow_ref_and_the_php_platform(self):
        current = upstream.read(upstream.WORKFLOW, upstream.DRUPACK_PIN)
        platform = upstream.read(upstream.COMPOSER, upstream.PLATFORM_PIN)
        self.serve_drupack("9.0.0", "php_version=8.5.99\nfrankenphp_version=1.12.7\n")
        summary = self.run_bumper(upstream.drupack)
        self.assertEqual(summary.rows, [("Drupack", current, "9.0.0", "bumped"),
                                        ("PHP platform", platform, "8.5.99", "bumped")])
        self.assertIn("build.yml@9.0.0\n", self.text(upstream.WORKFLOW))
        self.assertIn('"platform": {"php": "8.5.99"}', self.text(upstream.COMPOSER))

    def test_a_drupack_release_on_the_same_php_leaves_the_platform(self):
        current = upstream.read(upstream.WORKFLOW, upstream.DRUPACK_PIN)
        platform = upstream.read(upstream.COMPOSER, upstream.PLATFORM_PIN)
        self.serve_drupack("9.0.0", f"php_version={platform}\n")
        summary = self.run_bumper(upstream.drupack)
        self.assertEqual(summary.rows, [("Drupack", current, "9.0.0", "bumped")])

    def test_a_prerelease_number_past_nine_counts_as_newer(self):
        workflow = self.tree / upstream.WORKFLOW
        current = upstream.read(upstream.WORKFLOW, upstream.DRUPACK_PIN)
        workflow.write_text(workflow.read_text().replace(current, "1.0.0-alpha9"))
        self.serve_drupack("1.0.0-alpha10", "php_version=8.5.99\n")
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

    def test_a_drupack_release_naming_no_php_stops_the_run_before_any_write(self):
        self.serve_drupack("9.0.0", "frankenphp_version=1.12.7\n")
        before = {path: self.text(path) for path in PINNED}
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.drupack)
        self.assertEqual({path: self.text(path) for path in PINNED}, before)

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

    def test_a_held_package_is_reported_with_its_reason_and_left_alone(self):
        self.serve_composer(**{"twig/twig": ["v4.0.0-alpha1", "v3.30.0", "v3.29.0"]})
        summary = self.run_bumper(upstream.composer)
        self.assertEqual(self.pin("twig/twig"), "3.29.0")
        self.assertIn(f"HELD twig/twig 3.29.0 (3.30.0): {upstream.HOLDS['twig/twig']}", summary.notes)
        self.assertNotIn("twig/twig", [row[0] for row in summary.rows])

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

    def test_a_package_no_repository_lists_stops_the_run(self):
        self.serve_composer()
        del self.indexes[f"{upstream.DRUPAL_METADATA}drupal/canvas.json"]
        with self.assertRaises(SystemExit):
            self.run_bumper(upstream.composer)

    def test_a_pin_matching_nowhere_stops_the_run(self):
        (self.tree / upstream.WORKFLOW).write_text("jobs: {}\n")
        self.serve_drupack("9.0.0", "php_version=8.5.99\n")
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
