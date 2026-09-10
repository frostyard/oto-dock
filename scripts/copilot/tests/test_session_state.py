"""Real filesystem isolation and explicit state retention/disposal checks."""

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))

from core.layers.copilot.session_state import (  # noqa: E402
    PrivateCopilotSessionState, SANDBOX_STATE_DIRECTORY, SessionStateError,
)


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "trusted-state"
        self.root.mkdir(mode=0o700)

    def test_same_shared_workspace_never_selects_or_reuses_history(self):
        workspace = self.base / "shared-workspace"
        workspace.mkdir()
        first = PrivateCopilotSessionState.create(self.root)
        second = PrivateCopilotSessionState.create(self.root)
        try:
            self.assertNotEqual(first.path, second.path)
            for state in (first, second):
                self.assertEqual(state.path.parent, self.root)
                self.assertFalse(state.path.is_relative_to(workspace))
                self.assertEqual(list(state.path.iterdir()), [])
                self.assertEqual(state.sandbox_destination, SANDBOX_STATE_DIRECTORY)
                self.assertEqual(stat.S_IMODE(state.path.stat().st_mode), 0o700)
            (first.path / "history.json").write_text("first-account-only")
            self.assertFalse((second.path / "history.json").exists())
        finally:
            first.discard()
            second.discard()

    def test_retain_same_state_across_runtime_restart_then_explicit_cleanup(self):
        state = PrivateCopilotSessionState.create(self.root)
        mount_source = state.path
        history = mount_source / "session-history"
        history.write_text("resume marker")
        # Runtime owners only borrow this mount source. Stopping or replacing
        # those owners has no hook into the state allocation's lifetime.
        first_runtime_mount = state.path
        del first_runtime_mount
        second_runtime_mount = state.path
        self.assertEqual(second_runtime_mount, mount_source)
        self.assertEqual(history.read_text(), "resume marker")
        state.discard()
        state.discard()
        state.close()
        self.assertFalse(mount_source.exists())
        with self.assertRaises(SessionStateError):
            _ = state.path

    def test_mode_is_0700_even_with_restrictive_umask(self):
        previous = os.umask(0o777)
        try:
            state = PrivateCopilotSessionState.create(self.root)
        finally:
            os.umask(previous)
        try:
            self.assertEqual(stat.S_IMODE(state.path.stat().st_mode), 0o700)
        finally:
            state.close()

    def test_group_or_world_writable_roots_are_rejected(self):
        for mode in (0o770, 0o707, 0o777, 0o1777):
            with self.subTest(mode=oct(mode)):
                self.root.chmod(mode)
                with self.assertRaises(SessionStateError):
                    PrivateCopilotSessionState.create(self.root)
                self.assertEqual(list(self.root.iterdir()), [])
        self.root.chmod(0o700)

    def test_existing_root_owned_by_another_uid_is_rejected(self):
        with patch("core.layers.copilot.session_state.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(SessionStateError):
                PrivateCopilotSessionState.create(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_relative_traversal_and_nondirectory_roots_are_rejected(self):
        regular_file = self.base / "file"
        regular_file.write_text("untouched")
        for root in (self.base / "missing", Path("relative"),
                     self.root / ".." / self.root.name, regular_file):
            with self.subTest(root_kind=root.name), self.assertRaises(SessionStateError):
                PrivateCopilotSessionState.create(root)
        self.assertEqual(regular_file.read_text(), "untouched")

    def test_symlink_root_and_symlink_ancestor_are_rejected(self):
        link = self.base / "link"
        link.symlink_to(self.root, target_is_directory=True)
        nested = self.root / "nested"
        nested.mkdir(mode=0o700)
        for root in (link, link / "nested"):
            with self.subTest(root_kind=root.name), self.assertRaises(SessionStateError):
                PrivateCopilotSessionState.create(root)
        self.assertEqual(list(nested.iterdir()), [])

    def test_replaced_state_directory_is_not_deleted_or_adopted(self):
        state = PrivateCopilotSessionState.create(self.root)
        original = state.path
        retained = self.root / "retained"
        original.rename(retained)
        original.mkdir(mode=0o700)
        marker = original / "unrelated"
        marker.write_text("keep")
        try:
            with self.assertRaises(SessionStateError):
                _ = state.path
            with self.assertRaises(SessionStateError):
                state.discard()
            self.assertEqual(marker.read_text(), "keep")
        finally:
            marker.unlink()
            original.rmdir()
            retained.rename(original)
            state.discard()

    def test_replaced_root_is_not_deleted_or_adopted(self):
        state = PrivateCopilotSessionState.create(self.root)
        name = state.path.name
        retained = self.base / "retained-root"
        self.root.rename(retained)
        self.root.mkdir(mode=0o700)
        unrelated = self.root / name
        unrelated.mkdir(mode=0o700)
        marker = unrelated / "keep"
        marker.write_text("unrelated")
        try:
            with self.assertRaises(SessionStateError):
                state.discard()
            self.assertEqual(marker.read_text(), "unrelated")
            self.assertTrue((retained / name).is_dir())
        finally:
            marker.unlink()
            unrelated.rmdir()
            self.root.rmdir()
            retained.rename(self.root)
            state.discard()

    def test_symlink_replacement_and_symlink_contents_cannot_delete_unrelated_data(self):
        unrelated = self.base / "unrelated"
        unrelated.mkdir()
        marker = unrelated / "keep"
        marker.write_text("untouched")
        state = PrivateCopilotSessionState.create(self.root)
        original = state.path
        retained = self.root / "retained"
        original.rename(retained)
        original.symlink_to(unrelated, target_is_directory=True)
        try:
            with self.assertRaises(SessionStateError):
                state.discard()
            self.assertEqual(marker.read_text(), "untouched")
        finally:
            original.unlink()
            retained.rename(original)
        (state.path / "external-link").symlink_to(unrelated, target_is_directory=True)
        state.discard()
        self.assertEqual(marker.read_text(), "untouched")

    def test_permission_change_fails_closed_until_restored(self):
        state = PrivateCopilotSessionState.create(self.root)
        original = state.path
        original.chmod(0o755)
        try:
            with self.assertRaises(SessionStateError):
                _ = state.path
            with self.assertRaises(SessionStateError):
                state.close()
        finally:
            original.chmod(0o700)
            state.close()


if __name__ == "__main__":
    unittest.main()
