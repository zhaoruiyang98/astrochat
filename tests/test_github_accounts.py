import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from astrochat.github_commands import USAGE, GitHubCommands
from astrochat.project_store import ProjectError, open_projects


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.store = self.resources.enter_context(open_projects(self.path, "https://zulip/", "bot"))
        self.members = [
            {"user_id": 10, "full_name": "Alice"},
            {"user_id": 11, "full_name": "Bob Smith"},
            {"user_id": 12, "full_name": "张三"},
            {"user_id": 13, "full_name": "O'Brien"},
        ]
        self.client = Mock()
        self.client.get_members.return_value = {"result": "success", "members": self.members}
        self.commands = GitHubCommands(self.store, self.client)

    def test_self_link_change_and_unlink_record_actor_and_time(self):
        with patch("astrochat.project_store.time.time", return_value=1792454400):
            reply = self.commands.execute(10, "github link Alice-GH")
        self.assertIn("(not linked) → `alice-gh`", reply)
        self.assertIn("Alice (Zulip user ID 10)", reply)
        self.assertIn("2026-10-20 00:00:00 UTC", reply)
        self.assertEqual(self.store.github_account(10).updated_by, 10)
        self.assertIn(
            "`alice-gh` → `new-login`", self.commands.execute(10, "github link new-login")
        )
        self.assertIn("`new-login` → (not linked)", self.commands.execute(10, "github unlink"))
        self.assertIsNone(self.store.github_account(10).github_login)
        self.assertEqual(self.store.github_accounts(), [])
        self.assertIn("Last changed by Alice", self.commands.execute(10, "github status"))
        self.assertIn("(not linked)", self.commands.execute(10, "github status"))

    def test_any_user_can_set_view_replace_and_remove_another_users_mapping(self):
        self.commands.execute(10, "github link bob-gh @**Bob Smith**")
        self.assertEqual(self.store.github_account(11).github_login, "bob-gh")
        self.assertEqual(self.store.github_account(11).updated_by, 10)
        self.assertIn("`bob-gh`", self.commands.execute(12, "github status @**Bob Smith|11**"))
        reply = self.commands.execute(12, "github link another-login @_**Bob Smith|11**")
        self.assertIn("`bob-gh` → `another-login`", reply)
        self.assertEqual(self.store.github_account(11).updated_by, 12)
        reply = self.commands.execute(13, "github unlink @**Bob Smith**")
        self.assertIn("`another-login` → (not linked)", reply)
        self.assertEqual(self.store.github_account(11).updated_by, 13)
        self.assertIsNone(self.store.github_account(10))

    def test_mentions_preserve_spaces_unicode_quotes_and_explicit_ids(self):
        for mention, user_id in (("@**Bob Smith**", 11), ("@**张三**", 12), ("@**O'Brien**", 13)):
            with self.subTest(mention=mention):
                self.commands.execute(10, f"github link user-{user_id} {mention}")
                self.assertEqual(self.store.github_account(user_id).github_login, f"user-{user_id}")
        self.commands.execute(10, "github link renamed @**|11**")
        self.assertEqual(self.store.github_account(11).github_login, "renamed")

    def test_rejects_ambiguous_names_and_mismatched_or_missing_user_ids(self):
        self.members.append({"user_id": 14, "full_name": "Bob Smith"})
        self.assertIn("Several", self.commands.execute(10, "github link bob @**Bob Smith**"))
        self.commands.execute(10, "github link bob @**Bob Smith|14**")
        self.assertEqual(self.store.github_account(14).github_login, "bob")
        for mention in ("@**Bob Smith|11**", "@**Nobody|999**", "@**张三|11**"):
            reply = self.commands.execute(10, f"github link bob {mention}")
            self.assertNotIn("mapping updated", reply)
        self.assertIsNone(self.store.github_account(11))
        self.assertEqual(len(self.store.github_accounts()), 1)

    def test_case_insensitive_conflict_preserves_both_mappings_and_last_editor(self):
        self.commands.execute(10, "github link original")
        self.commands.execute(11, "github link Alice")
        previous = self.store.github_account(10)
        other = self.store.github_account(11)
        reply = self.commands.execute(12, "github link ALICE @**Alice**")
        self.assertIn("already linked to Zulip user ID 11", reply)
        self.assertEqual(self.store.github_account(10), previous)
        self.assertEqual(self.store.github_account(11), other)
        self.commands.execute(12, "github unlink @**Bob Smith**")
        self.commands.execute(12, "github link ALICE @**Alice**")
        self.assertEqual(self.store.github_account(10).github_login, "alice")

    def test_concurrent_claims_of_one_github_name_have_only_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = [
                executor.submit(self.store.set_github_account, user_id, login, 12)
                for user_id, login in ((10, "Alice"), (11, "ALICE"))
            ]
        self.assertEqual(sum(attempt.exception() is None for attempt in attempts), 1)
        errors = [attempt.exception() for attempt in attempts if attempt.exception()]
        self.assertIsInstance(errors[0], ProjectError)
        self.assertEqual(len(self.store.github_accounts()), 1)

    def test_status_and_list_show_current_names_and_all_users_without_extra_notifications(self):
        self.commands.execute(10, "github link alice")
        self.commands.execute(12, "github link bob @**Bob Smith**")
        self.members[1]["full_name"] = "Renamed Bob"
        self.client.get_members.reset_mock()
        reply = self.commands.execute(13, "github list")
        self.client.get_members.assert_called_once()
        self.assertIn("Alice (Zulip user ID 10) → `alice`", reply)
        self.assertIn("Renamed Bob (Zulip user ID 11) → `bob`", reply)
        self.assertIn("Last changed by 张三", reply)
        self.assertNotIn("@**", reply)
        self.assertIn("no GitHub account", self.commands.execute(13, "github status"))

    def test_invalid_inputs_do_not_write_or_echo_tokens(self):
        commands = [
            "github link",
            "github list extra",
            "github link @alice",
            "github link https://github.com/alice",
            "github link -alice",
            "github link alice-",
            "github link alice--bob",
            "github link " + "a" * 40,
            "github link ghp_secret",
            "github link alice Bob Smith",
            "github link alice @Bob",
            "github link alice @*group*",
            "github link alice @**all**",
            "github link alice @**Bob Smith** @**张三**",
            "github unlink @**Nobody**",
        ]
        for command in commands:
            with self.subTest(command=command):
                reply = self.commands.execute(10, command)
                self.assertNotIn("ghp_secret", reply)
                self.assertNotIn("mapping updated", reply)
        self.assertEqual(self.store.github_accounts(), [])
        self.assertIsNone(self.store.github_account(10))
        for command in ("github", "github help", "github unknown"):
            self.assertEqual(self.commands.execute(10, command), USAGE)

    def test_user_lookup_failures_do_not_mutate_or_echo_upstream_details(self):
        self.commands.execute(10, "github link original")
        previous = self.store.github_account(10)
        self.client.get_members.return_value = {"result": "error", "msg": "secret error details"}
        reply = self.commands.execute(10, "github unlink")
        self.assertIn("No mappings were changed", reply)
        self.assertNotIn("secret", reply)
        self.client.get_members.side_effect = requests.Timeout("secret error details")
        self.assertNotIn("secret", self.commands.execute(10, "github link another"))
        self.assertEqual(self.store.github_account(10), previous)

    def test_names_are_escaped_and_cannot_inject_notifications(self):
        self.members[0]["full_name"] = "@**all** [fake](https://example.com)"
        reply = self.commands.execute(10, "github link alice")
        self.assertNotIn("@**all**", reply)
        self.assertNotIn("[fake]", reply)
        self.assertIn("Zulip user ID 10", reply)

    def test_mappings_and_unlink_metadata_survive_restart_and_are_shared_across_projects(self):
        first = self.store.create(
            10,
            "research",
            "alice",
            "user",
            1,
            "https://github.com/users/alice/projects/1",
            "Research",
            20,
            900,
        )
        second = self.store.create(
            11,
            "other",
            "bob",
            "user",
            2,
            "https://github.com/users/bob/projects/2",
            "Other",
            21,
            900,
        )
        self.commands.execute(12, "github link alice @**Alice**")
        self.commands.execute(13, "github unlink @**Bob Smith**")
        accounts = [self.store.github_account(10), self.store.github_account(11)]
        self.resources.close()
        self.store = self.resources.enter_context(open_projects(self.path, "https://zulip/", "bot"))
        self.assertEqual([self.store.github_account(10), self.store.github_account(11)], accounts)
        self.assertEqual(self.store.get_owned(10, "research"), first)
        self.assertEqual(self.store.get_owned(11, "other"), second)
        self.assertEqual(self.store.list_owned(12), [])
        with self.assertRaises(ProjectError):
            self.store.get_owned(12, "research")

    def test_v1_registry_migration_preserves_projects_encrypted_tokens_and_tickets(self):
        project = self.store.create(
            10,
            "research",
            "alice",
            "user",
            1,
            "https://github.com/users/alice/projects/1",
            "Research",
            20,
            900,
        )
        self.store.authorize(project, "private-token", "P_1")
        ticket = self.store.issue_ticket(10, "research")
        project = self.store.get_owned(10, "research")
        self.store.connection.executescript("DROP TABLE github_accounts; PRAGMA user_version = 1;")
        self.resources.close()
        self.store = self.resources.enter_context(open_projects(self.path, "https://zulip/", "bot"))
        self.assertEqual(self.store.connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.store.get_owned(10, "research"), project)
        self.assertEqual(self.store.ticket_project(ticket), project)
        self.assertEqual(self.store.token_for(project), "private-token")
        self.assertEqual(self.store.github_accounts(), [])
        self.store.set_github_account(10, "alice", 11)
        self.assertEqual(self.store.github_account(10).updated_by, 11)

    def test_failed_migration_does_not_advance_schema_version(self):
        self.store.connection.execute("PRAGMA user_version = 1")
        self.resources.close()
        with self.assertRaises(sqlite3.OperationalError):
            with open_projects(self.path, "https://zulip/", "bot"):
                pass
        with sqlite3.connect(self.path / "projects.sqlite3") as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
