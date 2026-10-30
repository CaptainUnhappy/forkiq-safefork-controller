"""No network: real bare Git integration plus fail-closed policy tests."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.gitstore import GitStore, SyncBlocked, process_lock
from app.push_guard import matches
from app.safefork import make_plan, synchronize, validate_config, validate_identity, valid_ref, repo_name

A, B, C = "a" * 40, "b" * 40, "c" * 40
MAIN = "refs/heads/main"


def config(**changes):
    value = dict(source="upstream/project", target="owner/project-SafeFork", source_id=11, target_id=22,
                 mode="public-fork", primary="main", branch_map={}, preserved_tags={})
    value.update(changes)
    return value


def metadata(cfg):
    private = cfg["mode"] == "standalone-private"
    source = dict(id=11, full_name=cfg["source"], private=private,
                  visibility="private" if private else "public", default_branch="main")
    target = dict(id=22, full_name=cfg["target"], private=private,
                  visibility="private" if private else "public", fork=not private, archived=False)
    if not private:
        target["parent"] = dict(id=11, full_name=cfg["source"])
    return source, target


class MemoryStore:
    def __init__(self, root=None, source=None, target=None, *tokens):
        self.state = {"source": {MAIN: B}, "target": {MAIN: A}}
        self.writes = []
        self.is_ancestor = Mock(return_value=True)
        self.files = Mock(return_value={"README.md", "code.py"})
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        pass
    def refs(self, side):
        return self.state[side].copy()
    def fetch(self, side, snapshot):
        pass
    def push(self, updates, expected):
        self.writes.append(updates.copy())
        self.state["target"].update(updates)


class PolicyTests(unittest.TestCase):
    def test_dry_run_never_pushes(self):
        store = MemoryStore()
        plan = synchronize(config(), store, lambda: None)
        self.assertEqual(plan["updates"], {MAIN: B})
        self.assertEqual(store.writes, [])

    def test_equal_and_target_only_refs_never_delete(self):
        store = MemoryStore()
        store.state["source"][MAIN] = A
        store.state["target"]["refs/heads/archive"] = C
        plan = synchronize(config(), store, lambda: None, dry_run=False)
        self.assertEqual(plan["target_only"], ["refs/heads/archive"])
        self.assertEqual(store.writes, [])

    def test_non_fast_forward_blocks_entire_batch(self):
        store = MemoryStore()
        store.state["source"]["refs/heads/aaa-new"] = C
        store.is_ancestor.return_value = False
        with self.assertRaisesRegex(SyncBlocked, "不是快进"):
            synchronize(config(), store, lambda: None, dry_run=False)
        self.assertEqual(store.writes, [])

    def test_missing_primary_or_control_collision(self):
        for refs in ({}, {MAIN: A, "refs/heads/sync-control": A}):
            with self.subTest(refs=refs), self.assertRaises(SyncBlocked):
                make_plan(config(), refs, {}, MemoryStore())

    def test_immutable_tag_and_exact_exception(self):
        ref = "refs/tags/v1"
        source, target = {MAIN: A, ref: B}, {MAIN: A, ref: C}
        with self.assertRaisesRegex(SyncBlocked, "标签冲突"):
            make_plan(config(), source, target, MemoryStore())
        cfg = config(preserved_tags={"v1": [C, B]})
        plan = make_plan(cfg, source, target, MemoryStore())
        self.assertEqual(plan["updates"], {})
        self.assertEqual(plan["decisions"][-1]["action"], "preserved")
        source[ref] = A
        with self.assertRaises(SyncBlocked):
            make_plan(cfg, source, target, MemoryStore())
        del source[ref]
        with self.assertRaises(SyncBlocked):
            make_plan(cfg, source, target, MemoryStore())

    def test_mapping_keeps_old_main(self):
        cfg = config(branch_map={"main": "upstream-main"})
        plan = make_plan(cfg, {MAIN: B}, {MAIN: A}, MemoryStore())
        self.assertEqual(plan["updates"], {"refs/heads/upstream-main": B})
        self.assertEqual(plan["target_only"], [MAIN])

    def test_mapping_collision_and_namespace_conflict(self):
        cases = [(config(branch_map={"main": "upstream-main"}),
                  {MAIN: A, "refs/heads/upstream-main": B}, {}),
                 (config(), {MAIN: A, "refs/heads/x/a": B}, {"refs/heads/x": C}),
                 (config(), {MAIN: A, "refs/tags/v/a": B}, {"refs/tags/v": C})]
        for cfg, source, target in cases:
            with self.subTest(source=source), self.assertRaises(SyncBlocked):
                make_plan(cfg, source, target, MemoryStore())

    def test_large_or_empty_deletion_blocks(self):
        for old, new in [({"a", "b", "c"}, {"a"}), ({"a"}, set()),
                         ({str(i) for i in range(300)}, {str(i) for i in range(150)})]:
            store = MemoryStore()
            store.files.side_effect = lambda sha: old if sha == A else new
            with self.subTest(new=len(new)), self.assertRaises(SyncBlocked):
                synchronize(config(), store, lambda: None, dry_run=False)
            self.assertEqual(store.writes, [])

    def test_source_or_target_changes_before_write(self):
        for side in ("source", "target"):
            store, calls = MemoryStore(), []
            def guard():
                calls.append(1)
                if len(calls) == 3:
                    store.state[side][MAIN] = C
            with self.subTest(side=side), self.assertRaises(SyncBlocked):
                synchronize(config(), store, guard, dry_run=False)
            self.assertEqual(store.writes, [])

    def test_identity_gate_failure_is_zero_write(self):
        store = MemoryStore()
        with self.assertRaises(SyncBlocked):
            synchronize(config(), store, Mock(side_effect=SyncBlocked("Actions enabled")), dry_run=False)
        self.assertEqual(store.writes, [])

    def test_before_write_failure_is_zero_write(self):
        store = MemoryStore()
        with self.assertRaises(SyncBlocked):
            synchronize(config(), store, lambda: None, dry_run=False,
                        before_write=Mock(side_effect=SyncBlocked("paused")))
        self.assertEqual(store.writes, [])

    @patch("app.safefork.time.sleep")
    def test_unconfirmed_push_does_not_retry(self, sleep):
        store = MemoryStore()
        store.push = Mock()
        with self.assertRaisesRegex(SyncBlocked, "未完全确认"):
            synchronize(config(), store, lambda: None, dry_run=False)
        store.push.assert_called_once()

    def test_public_and_private_identity(self):
        for cfg in (config(), config(mode="standalone-private", authorized=True, source_secret="read_source")):
            source, target = metadata(cfg)
            validate_identity(cfg, source, target)
            for side, key, value in [(source, "id", 999), (source, "default_branch", "new-main"),
                                     (source, "private", not source["private"]),
                                     (target, "visibility", "internal"), (target, "archived", True),
                                     (target, "fork", not target["fork"])]:
                changed = copy.deepcopy(side)
                changed[key] = value
                with self.subTest(mode=cfg["mode"], key=key), self.assertRaises(SyncBlocked):
                    validate_identity(cfg, changed if side is source else source,
                                      changed if side is target else target)

    def test_private_authorization_and_parent_identity(self):
        with self.assertRaises(SyncBlocked):
            validate_config(config(mode="standalone-private", source_secret="source"))
        cfg = config()
        source, target = metadata(cfg)
        target["parent"]["id"] = 999
        with self.assertRaises(SyncBlocked):
            validate_identity(cfg, source, target)

    def test_mapping_and_ref_validation(self):
        for mapping in ({"main": "sync-control"}, {"main": "main"}, {"main": "x", "x": "y"},
                        {"main": "x", "other": "x"}, {"main": "x.lock"}):
            with self.subTest(mapping=mapping), self.assertRaises(SyncBlocked):
                validate_config(config(branch_map=mapping))
        for ref in ("refs/heads/x..y", "refs/heads/.x", "refs/heads/x.lock", "refs/heads/x y",
                    "refs/heads/x@{1}", "refs/heads/x?", "refs/heads/x\\y", "refs/heads//x"):
            self.assertFalse(valid_ref(ref), ref)
        for name in ("https://github.com/a/b", "a/b?secret", "a/b/c", "a/..", "-a/b"):
            with self.assertRaises(SyncBlocked):
                repo_name(name)

    def test_push_hook_rejects_changed_or_deleted_remote_ref(self):
        expected = {MAIN: [A, B]}
        self.assertTrue(matches(expected, [f"{B} {B} {MAIN} {A}\n"]))
        for line in (f"{B} {B} {MAIN} {C}", f"{B} {B} {MAIN} {'0'*40}",
                     f"(delete) {'0'*40} {MAIN} {A}", "malformed"):
            self.assertFalse(matches(expected, [line]))

    def test_credentials_only_in_child_environment(self):
        store = GitStore(".", "a/b", "c/d", lambda: "", lambda: "secret-fixture")
        with patch.dict(os.environ, {"GIT_TRACE": "1", "GIT_CONFIG_COUNT": "9", "GIT_SSH_COMMAND": "bad"}):
            env = store.env("source")
            self.assertNotIn("GIT_TRACE", env)
            self.assertNotIn("GIT_CONFIG_COUNT", env)
            env = store.env("target")
            self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
            self.assertNotIn("secret-fixture", str(store.__dict__["target"]))
        with patch("app.gitstore.shutil.disk_usage", return_value=Mock(free=10)):
            with self.assertRaisesRegex(SyncBlocked, "磁盘"):
                store.check_space()

    def test_writer_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "writer.lock"
            with process_lock(path), self.assertRaises(SyncBlocked):
                with process_lock(path):
                    pass


class RealGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.target = self.root / "target.git"
        self.source.mkdir()
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.name", "Test Fixture")
        self.git(self.source, "config", "user.email", "fixture@example.invalid")
        self.a = self.commit("first")
        self.git(self.root, "init", "--bare", str(self.target))
        self.git(self.source, "push", str(self.target), "main:main")
        paths = {"source": self.source.as_uri(), "target": self.target.as_uri()}
        class LocalStore(GitStore):
            def url(self, side):
                return paths[side]
            def env(self, side=None):
                env = super().env(None)
                env.update(GIT_ALLOW_PROTOCOL="file", GIT_CONFIG_COUNT="1",
                           GIT_CONFIG_KEY_0="protocol.file.allow", GIT_CONFIG_VALUE_0="always")
                return env
        self.Store = LocalStore

    def git(self, cwd, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
        result = subprocess.run(["git", "-c", "core.hooksPath=" + str(self.root / "no-hooks"), *args],
                                cwd=cwd, env=env, text=True, encoding="utf-8", capture_output=True, check=True)
        return result.stdout.strip()

    def commit(self, text):
        (self.source / "README.md").write_text(text, encoding="utf-8")
        self.git(self.source, "add", "README.md")
        self.git(self.source, "commit", "-m", text)
        return self.git(self.source, "rev-parse", "HEAD")

    def store(self):
        return self.Store(self.root, "unused/source", "unused/target")

    def test_full_history_branches_raw_annotated_tag_and_archive(self):
        b = self.commit("second")
        self.git(self.source, "branch", "feature", b)
        self.git(self.source, "tag", "-a", "v1", "-m", "annotated")
        tag = self.git(self.source, "rev-parse", "refs/tags/v1")
        self.git(self.target, "update-ref", "refs/heads/archive", self.a)
        with self.store() as store:
            plan = synchronize(config(), store, lambda: None, dry_run=False)
            refs = store.refs("target")
            self.assertEqual(refs[MAIN], b)
            self.assertEqual(refs["refs/heads/feature"], b)
            self.assertEqual(refs["refs/tags/v1"], tag)
            self.assertNotEqual(tag, b)
            self.assertEqual(refs["refs/heads/archive"], self.a)
            self.assertEqual(len(plan["updates"]), 3)
        self.assertEqual(self.git(self.target, "rev-list", "--count", "main"), "2")

    def test_dry_run_with_real_git_leaves_target_exactly_unchanged(self):
        self.commit("second")
        self.git(self.source, "branch", "new")
        with self.store() as store:
            before = store.refs("target")
            synchronize(config(), store, lambda: None)
            self.assertEqual(store.refs("target"), before)

    def test_workflow_files_preserve_git_objects_without_execution(self):
        workflow = self.source / ".github" / "workflows" / "fixture.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: Harmless fixture\non: [push]\njobs:\n  marker:\n    runs-on: ubuntu-latest\n"
                            "    steps:\n      - run: echo fixture > untrusted-marker\n", encoding="utf-8")
        self.git(self.source, "add", ".github/workflows/fixture.yml")
        self.commit("workflow fixture")
        with self.store() as store:
            synchronize(config(), store, lambda: None, dry_run=False)
        ref = "main:.github/workflows/fixture.yml"
        self.assertEqual(self.git(self.source, "rev-parse", ref), self.git(self.target, "rev-parse", ref))
        self.assertFalse((self.root / "untrusted-marker").exists())
        self.assertFalse((self.source / "untrusted-marker").exists())

    def test_upstream_rollback_never_moves_target(self):
        b = self.commit("second")
        self.git(self.source, "push", str(self.target), "main:main")
        self.git(self.source, "update-ref", MAIN, self.a)
        with self.store() as store:
            with self.assertRaisesRegex(SyncBlocked, "不是快进"):
                synchronize(config(), store, lambda: None, dry_run=False)
            self.assertEqual(store.refs("target")[MAIN], b)

    def test_fetch_snapshot_race_aborts(self):
        with self.store() as store:
            snapshot = store.refs("source")
            self.commit("second")
            with self.assertRaisesRegex(SyncBlocked, "引用变化"):
                store.fetch("source", snapshot)

    def test_pre_push_guard_blocks_remote_rewind_even_if_push_would_fast_forward(self):
        b = self.commit("second")
        self.git(self.source, "push", str(self.target), "main:main")
        c = self.commit("third")
        with self.store() as store:
            source, target = store.refs("source"), store.refs("target")
            store.fetch("source", source)
            store.fetch("target", target)
            self.assertEqual(target[MAIN], b)
            self.git(self.target, "update-ref", MAIN, self.a)
            with self.assertRaises(SyncBlocked):
                store.push({MAIN: c, "refs/heads/new": c}, target)
            self.assertEqual(store.refs("target"), {MAIN: self.a})

    def test_atomic_push_rejection_leaves_every_ref_unchanged(self):
        b = self.commit("second")
        self.git(self.target, "config", "receive.denyNonFastForwards", "true")
        hooks = self.target / "hooks"
        reject = hooks / "update"
        reject.write_text('#!/bin/sh\n[ "$1" != "refs/heads/new" ]\n', newline="\n")
        reject.chmod(0o700)
        with self.store() as store:
            source, target = store.refs("source"), store.refs("target")
            store.fetch("source", source)
            store.fetch("target", target)
            with self.assertRaises(SyncBlocked):
                store.push({MAIN: b, "refs/heads/new": b}, target)
            self.assertEqual(store.refs("target"), target)

    def test_mapping_can_follow_rewritten_upstream_without_touching_archive(self):
        old = self.commit("old lineage")
        self.git(self.source, "push", str(self.target), "main:main")
        self.git(self.source, "checkout", "--orphan", "new-history")
        new = self.commit("new lineage")
        self.git(self.source, "update-ref", MAIN, new)
        self.git(self.source, "update-ref", "-d", "refs/heads/new-history")
        with self.store() as store:
            synchronize(config(branch_map={"main": "upstream-main"}), store, lambda: None, dry_run=False)
            refs = store.refs("target")
            self.assertEqual(refs[MAIN], old)
            self.assertEqual(refs["refs/heads/upstream-main"], new)


if __name__ == "__main__":
    unittest.main()
