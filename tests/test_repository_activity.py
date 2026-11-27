"""Repository observations use real refs and worktrees without changing an inspected checkout."""

from datetime import datetime
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/admin"))
from code_sync import CodeSync, default_policy
from repository_activity import ActivityMonitor, ZONE, activity_scope, candidates, observe, slot
from sync_runtime import git, text_git


class RepositoryActivityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.workspace = root / "projects"
        self.project = self.workspace / "sample"
        self.project.mkdir(parents=True)
        self.hub = CodeSync(root / "state")
        self.hub.import_policy({**default_policy(), "source_roots": [str(self.workspace)]})
        git(self.project, "init", "-b", "main")
        git(self.project, "config", "user.name", "test")
        git(self.project, "config", "user.email", "test@example.invalid")
        git(self.project, "commit", "--allow-empty", "-m", "base")
        git(self.project, "remote", "add", "origin", "https://github.com/example/project.git")
        self.base = text_git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/main", self.base)
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.projects = {
            "one": {"id": "one", "name": "One", "directory": str(self.project)},
            "two": {"id": "two", "name": "Two", "directory": str(self.project)},
        }
        registry = patch.object(self.hub.registry, "read_hub", return_value={"projects": self.projects})
        registry.start()
        self.addCleanup(registry.stop)
        self.feature = self.workspace / "feature"
        git(self.project, "worktree", "add", "-b", "feature", str(self.feature))
        git(self.feature, "commit", "--allow-empty", "-m", "feature")
        (self.feature / "note").write_text("pending")
        self.tip = text_git(self.feature, "rev-parse", "HEAD")
        self.detached = self.workspace / "snapshot"
        git(self.project, "worktree", "add", "--detach", str(self.detached))
        (self.detached / "scratch").touch()
        self.remote = {
            "default": "main",
            "heads": {"main": self.base, "feature": self.base, "remote-only": self.base},
            "prs": [{"name": "feature", "number": 4, "base": "main", "updated": time.time()}],
            "merged": {},
            "notes": [],
        }

    def inspect(self, remote=None):
        groups, roots = candidates(self.hub)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["projects"], ["One", "Two"])
        return observe(self.hub, groups[0], roots, time.time(), lambda *_: remote or self.remote)

    def test_counts_deduplicate_reasons_and_exclude_detached_and_old_commits(self):
        # An inactive, unmerged branch remains pending outside the 7-day window.
        git(self.project, "switch", "-c", "old")
        git(
            self.project,
            "commit",
            "--allow-empty",
            "-m",
            "old",
            env={"GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"},
        )
        git(self.project, "switch", "main")
        index = self.project / ".git/index"
        original = index.read_bytes()
        d = self.inspect()
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(text_git(self.project, "rev-parse", "HEAD"), self.base)
        s = d["summary"]
        self.assertEqual(
            (s["branch"], s["target"], s["active"], s["pending"], s["worktree_count"]),
            ("main", "main", 1, 2, 3),
        )
        branches = {b["name"]: b for b in d["branches"]}
        self.assertEqual(len(branches["feature"]["reasons"]), 3)
        self.assertEqual(branches["feature"]["push"], "本地有待推送提交")
        self.assertEqual(branches["old"]["reasons"], [])
        self.assertEqual(branches["remote-only"]["merge"], "merged")
        self.assertEqual(next(t for t in d["worktrees"] if not t["branch"])["dirty"], True)
        self.remote["merged"] = {(self.tip, "main"): 17}
        self.assertEqual(self.inspect()["summary"]["pending"], 1)

    def test_branch_difference_exposes_local_and_remote_tips_and_excludes_current_branch_from_parallel(self):
        git(self.project, "commit", "--allow-empty", "-m", "development update")
        advanced = text_git(self.project, "rev-parse", "HEAD")
        self.remote["heads"]["main"] = advanced
        d = self.inspect()
        feature = next(b for b in d["branches"] if b["name"] == "feature")
        counts = {row["scope"]: (row["ahead"], row["behind"]) for row in feature["differences"]}
        self.assertEqual(counts, {"本地": (1, 1), "GitHub": (0, 1)})
        self.assertEqual(d["summary"]["parallel"], 1)

        self.projects["one"]["directory"] = str(self.feature)
        self.projects["two"]["directory"] = str(self.feature)
        d = self.inspect()
        self.assertEqual(d["summary"]["branch"], "feature")
        self.assertEqual(d["summary"]["active"], 1)
        self.assertEqual(d["summary"]["parallel"], 0)

    def test_registered_alias_uses_only_the_granted_real_checkout(self):
        alias = self.workspace.parent / "registered"
        alias.symlink_to(self.project)
        self.projects["two"]["directory"] = str(alias)
        groups, _ = candidates(self.hub)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["issues"], {})
        forbidden = self.workspace / "outside"
        forbidden.symlink_to(self.workspace.parent)
        self.projects["two"]["directory"] = str(forbidden)
        groups, _ = candidates(self.hub)
        rejected = next(g for g in groups if "Two" in g["projects"])
        self.assertFalse(rejected["roots"])
        self.assertIn("授权", rejected["issues"][str(forbidden)])

    def test_missing_remote_and_target_are_unknown(self):
        unavailable = {"default": "", "heads": None, "prs": None, "merged": {}, "notes": ["无法连接"]}
        summary = self.inspect(unavailable)["summary"]
        self.assertEqual(summary["target"], "main")
        self.assertTrue(summary["partial"])
        git(self.project, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        d = self.inspect(unavailable)
        self.assertIsNone(d["summary"]["pending"])
        self.assertEqual(d["summary"]["active"], 1)
        self.assertFalse(next(b for b in d["branches"] if b["name"] == "feature")["prs_known"])

    def test_release_compares_published_development_history_and_preserves_legacy_settings(self):
        group = candidates(self.hub)[0][0]
        key = group["id"]
        self.remote["heads"].update(develop=self.tip, release=self.base)
        activity_scope(self.hub, key, {"branch": "develop", "release": "release", "request": "first"})
        d = self.inspect()
        self.assertEqual(d["summary"]["release"]["count"], 1)
        self.assertEqual(d["summary"]["release"]["basis"], "GitHub")
        self.assertEqual(
            d["release_commits"],
            [{"sha": self.tip, "at": d["release_commits"][0]["at"], "subject": "feature"}],
        )
        activity_scope(self.hub, key, {"branch": "main", "request": "legacy"})
        self.assertEqual(self.inspect()["summary"]["release"]["count"], 0)
        self.assertEqual(activity_scope(self.hub, key), {"branch": "main", "release": "release"})
        del self.remote["heads"]["release"]
        self.assertIsNone(self.inspect()["summary"]["release"]["count"])
        activity_scope(self.hub, key, {"branch": "main", "release": "", "request": "clear"})
        self.assertNotIn("release", self.inspect()["summary"])

    def test_schedule_boundary_override_and_published_snapshot(self):
        before = datetime(2026, 11, 27, 1, 59, 59, tzinfo=ZONE).timestamp()
        self.assertEqual(slot(before), "2026-11-26")
        self.assertEqual(slot(before + 1), "2026-11-27")
        monitor = ActivityMonitor(self.hub, lambda *_: self.remote)
        monitor.collect()
        s = monitor.status()["repositories"][0]
        self.hub.store.metadata("activity-target:" + s["id"], "feature")
        self.hub.store.metadata("activity-request", "next")
        self.assertTrue(monitor.status()["pending"])
        monitor.collect()
        self.assertFalse(monitor.status()["pending"])
        self.assertEqual(monitor.status()["repositories"][0]["target"], "feature")
        self.projects["missing"] = {
            "id": "missing",
            "name": "Missing",
            "directory": str(self.workspace / "gone"),
        }
        monitor.collect()
        missing = next(s for s in monitor.status()["repositories"] if s["name"] == "Missing")
        self.assertIsNone(missing["active"])
