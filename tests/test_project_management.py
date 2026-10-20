import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import requests

from astrochat.project_commands import (
    USAGE,
    ProjectCommands,
    SyncRunner,
    auth_origin,
    parse_project_url,
)
from astrochat.project_store import ProjectError, open_projects
from astrochat.project_sync import SyncResult
from astrochat.sync_worker import SyncWorker


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.store = self.resources.enter_context(open_projects(self.path, "https://zulip/", "bot"))

    def create(self, user=10, name="research", channel=20, number=1):
        return self.store.create(
            user,
            name,
            "alice",
            "user",
            number,
            f"https://github.com/users/alice/projects/{number}",
            "Research",
            channel,
            900,
        )

    def authorize(self, project, github_id="P_1", token="github-secret-token"):
        self.store.authorize(project, token, github_id)
        return self.store.get_owned(project.user_id, project.name)


class ProjectStoreTests(ProjectFixture):
    def test_multiple_projects_and_same_names_are_isolated_by_user(self):
        alice = self.create()
        self.create(name="second", number=2)
        bob = self.create(user=11)
        self.assertNotEqual(alice.id, bob.id)
        self.assertEqual(len(self.store.list_owned(10)), 2)
        self.assertEqual(len(self.store.list_owned(11)), 1)
        self.store.change_owned(11, "research", enabled=False)
        self.assertTrue(self.store.get_owned(10, "research").enabled)
        self.store.remove_owned(11, "research")
        self.assertEqual(self.store.get_owned(10, "research").id, alice.id)

    def test_foreign_user_cannot_resolve_owned_project_change_remove_authorize_or_sync(self):
        project = self.create()
        for operation in (
            lambda: self.store.get_owned(11, "research"),
            lambda: self.store.change_owned(11, "research", channel_id=99),
            lambda: self.store.remove_owned(11, "research"),
            lambda: self.store.issue_ticket(11, "research"),
            lambda: self.store.request_owned(11, "research"),
        ):
            with self.subTest(operation=operation), self.assertRaises(ProjectError):
                operation()
        self.assertEqual(self.store.list_owned(11), [])
        self.assertEqual(self.store.get_owned(10, "research"), project)

    def test_tokens_are_encrypted_and_survive_restart_with_the_key(self):
        project = self.authorize(self.create())
        self.assertNotIn("secret", repr(project))
        self.assertNotIn(b"github-secret-token", (self.path / "projects.sqlite3").read_bytes())
        self.resources.close()
        self.store = self.resources.enter_context(open_projects(self.path, "https://zulip/", "bot"))
        self.assertEqual(
            self.store.token_for(self.store.get_owned(10, "research")), "github-secret-token"
        )
        self.assertEqual((self.path / "token.key").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.path / "projects.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_registry_lock_identity_binding_and_lost_key(self):
        with self.assertRaises(BlockingIOError):
            with open_projects(self.path, "https://zulip/", "bot"):
                pass
        self.resources.close()
        with self.assertRaises(ValueError):
            with open_projects(self.path, "https://another-realm/", "bot"):
                pass
        (self.path / "token.key").unlink()
        with self.assertRaisesRegex(ValueError, "Missing token.key"):
            with open_projects(self.path, "https://zulip/", "bot"):
                pass

    def test_ticket_expiry_hashing_and_replacement(self):
        self.create()
        first = self.store.issue_ticket(10, "research", now=100)
        self.assertEqual(self.store.ticket_project(first, now=699).user_id, 10)
        with self.assertRaises(ProjectError):
            self.store.ticket_project(first, now=700)
        second = self.store.issue_ticket(10, "research", now=200)
        with self.assertRaises(ProjectError):
            self.store.ticket_project(first, now=201)
        self.assertEqual(self.store.ticket_project(second, now=201).name, "research")
        self.assertNotIn(second.encode(), (self.path / "projects.sqlite3").read_bytes())

    def test_concurrent_ticket_consumption_only_succeeds_once(self):
        self.create()
        ticket = self.store.issue_ticket(10, "research")
        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = [executor.submit(self.store.ticket_project, ticket, True) for _ in range(2)]
        self.assertEqual(sum(attempt.exception() is None for attempt in attempts), 1)

    def test_config_change_or_new_link_invalidates_inflight_authorization(self):
        self.create()
        for change in (
            lambda: self.store.change_owned(10, "research", enabled=False),
            lambda: self.store.issue_ticket(10, "research"),
        ):
            ticket = self.store.issue_ticket(10, "research")
            pending = self.store.ticket_project(ticket, consume=True)
            change()
            with self.assertRaises(ProjectError):
                self.store.authorize(pending, "secret", "P_1")
            self.assertFalse(self.store.get_owned(10, "research").authorized)

    def test_project_channel_collision_is_rejected_without_changing_other_users(self):
        first = self.authorize(self.create())
        second = self.create(user=11)
        with self.assertRaises(ProjectError):
            self.authorize(second)
        self.assertFalse(self.store.get_owned(11, "research").authorized)
        second = self.store.change_owned(11, "research", channel_id=30)
        self.authorize(second)
        with self.assertRaises(ProjectError):
            self.store.change_owned(11, "research", channel_id=20)
        self.assertEqual(self.store.get_owned(11, "research").channel_id, 30)
        self.assertEqual(self.store.get_owned(10, "research"), first)

    def test_pause_pending_and_unauthorized_projects_are_not_scheduled(self):
        project = self.create()
        self.assertIsNone(self.store.claim_due(time.time()))
        with self.assertRaises(ProjectError):
            self.store.request_owned(10, "research")
        project = self.authorize(project)
        self.store.change_owned(10, "research", enabled=False)
        self.assertIsNone(self.store.claim_due(time.time()))
        with self.assertRaises(ProjectError):
            self.store.request_owned(10, "research")
        project = self.store.change_owned(10, "research", enabled=True)
        self.assertEqual(self.store.claim_due(100).id, project.id)
        self.store.finish(project, "done", now=100)
        self.assertIsNone(self.store.claim_due(999))
        self.assertEqual(self.store.claim_due(1000).id, project.id)

    def test_manual_request_during_run_survives_completion(self):
        self.authorize(self.create())
        running = self.store.claim_due(100)
        self.store.request_owned(10, "research")
        self.store.finish(running, "done", now=101)
        self.assertEqual(self.store.claim_due(102).id, running.id)

    def test_old_completion_never_overwrites_changed_or_removed_project(self):
        project = self.authorize(self.create())
        changed = self.store.change_owned(10, "research", channel_id=30)
        self.store.finish(project, "outdated", now=100)
        self.assertEqual(self.store.get_owned(10, "research"), changed)
        ticket = self.store.issue_ticket(10, "research")
        self.store.remove_owned(10, "research")
        self.store.finish(changed, "outdated", now=100)
        with self.assertRaises(ProjectError):
            self.store.ticket_project(ticket)


class CommandTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.client = Mock()
        self.client.get_stream_id.return_value = {"result": "success", "stream_id": 20}
        self.client.call_endpoint.return_value = {"result": "success", "subscribers": [1, 10, 11]}
        self.client.get_members.return_value = {
            "result": "success",
            "members": [
                {"user_id": 10, "full_name": "Alice"},
                {"user_id": 11, "full_name": "Bob"},
            ],
        }
        self.worker = Mock()
        self.commands = ProjectCommands(
            self.store, self.worker, self.client, 1, "https://auth.example.com"
        )

    def test_setup_produces_only_a_private_authorization_link_and_persists_settings(self):
        reply = self.commands.execute(
            10, 'project setup Research https://github.com/users/Alice/projects/2 "My Channel" 300'
        )
        self.assertIn("https://auth.example.com/authorize#", reply)
        project = self.store.get_owned(10, "research")
        self.assertEqual(
            (project.owner, project.number, project.channel, project.interval_seconds),
            ("Alice", 2, "My Channel", 300),
        )
        self.assertFalse(project.authorized)
        self.client.get_stream_id.assert_called_once_with("My Channel")

    def test_management_commands_enforce_owner_before_any_api_request(self):
        self.authorize(self.create())
        ticket = self.store.issue_ticket(10, "research")
        project = self.store.get_owned(10, "research")
        for command in (
            "sync research",
            "auth research",
            "pause research",
            "resume research",
            "remove research",
            "channel research other",
            "interval research 60",
        ):
            with self.subTest(command=command):
                reply = self.commands.execute(11, "project " + command)
                self.assertIn("not found", reply)
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.worker.mock_calls, [])
        self.assertEqual(self.store.get_owned(10, "research"), project)
        self.assertEqual(self.store.ticket_project(ticket), project)

    def test_list_and_status_default_to_the_senders_own_projects(self):
        self.create()
        self.create(user=11, number=2)
        self.create(user=11, name="second", number=3)
        for command in ("project list", "project status", "project status ReSeArCh"):
            for viewer, own, other in ((10, "Alice", "Bob"), (11, "Bob", "Alice")):
                with self.subTest(command=command, viewer=viewer):
                    reply = self.commands.execute(viewer, command)
                    self.assertEqual(reply.count("**research**"), 1)
                    self.assertIn(f"Creator: {own}", reply)
                    self.assertNotIn(f"Creator: {other}", reply)
                    if viewer == 11 and command in ("project list", "project status"):
                        self.assertIn("**second**", reply)
                    else:
                        self.assertNotIn("**second**", reply)
        self.assertIn("not found", self.commands.execute(10, "project status second"))
        self.assertEqual(self.worker.mock_calls, [])

    def test_all_flag_shows_everyones_projects_without_exposing_credentials(self):
        self.authorize(self.create())
        ticket = self.store.issue_ticket(10, "research")
        self.create(user=11)
        self.create(user=11, name="second", number=2)
        self.store.change_owned(11, "second", enabled=False)
        before = self.store.list_projects()
        for command in ("project list --all", "project status --all"):
            for viewer in (10, 11, 99):
                with self.subTest(command=command, viewer=viewer):
                    self.client.reset_mock()
                    reply = self.commands.execute(viewer, command)
                    self.assertEqual(reply.count("**research**"), 2)
                    self.assertIn("**second** — paused", reply)
                    self.assertIn("Creator: Alice (Zulip user ID 10)", reply)
                    self.assertIn("Creator: Bob (Zulip user ID 11)", reply)
                    self.assertIn("Channel: Research", reply)
                    self.assertIn("Authorized; waiting to sync.", reply)
                    self.assertNotIn("github-secret-token", reply)
                    self.assertNotIn(ticket, reply)
                    self.assertNotIn("/authorize#", reply)
                    self.client.get_members.assert_called_once_with()
        self.assertEqual(self.store.list_projects(), before)
        self.assertEqual(self.store.ticket_project(ticket).user_id, 10)
        self.assertEqual(self.worker.mock_calls, [])

    def test_status_name_with_all_flag_shows_every_match_in_either_argument_order(self):
        self.create()
        self.create(user=11, number=2)
        self.create(name="different", number=3)
        for command in ("project status ReSeArCh --all", "project status --all ReSeArCh"):
            for viewer in (10, 99):
                with self.subTest(command=command, viewer=viewer):
                    reply = self.commands.execute(viewer, command)
                    self.assertEqual(reply.count("**research**"), 2)
                    self.assertIn("Creator: Alice", reply)
                    self.assertIn("Creator: Bob", reply)
                    self.assertIn("https://github.com/users/alice/projects/1", reply)
                    self.assertIn("https://github.com/users/alice/projects/2", reply)
                    self.assertNotIn("different", reply)
        self.store.remove_owned(10, "research")
        self.assertIn("not found", self.commands.execute(10, "project status research"))
        reply = self.commands.execute(10, "project status research --all")
        self.assertIn("Creator: Bob", reply)
        self.assertNotIn("Creator: Alice", reply)

    def test_empty_list_and_unmatched_status_do_not_fetch_users(self):
        self.assertIn("No projects", self.commands.execute(99, "project list --all"))
        self.assertIn("No projects", self.commands.execute(99, "project status --all"))
        self.create()
        self.assertIn("You have no projects", self.commands.execute(99, "project list"))
        self.assertIn("You have no projects", self.commands.execute(99, "project status"))
        self.assertIn("not found", self.commands.execute(99, "project status research"))
        self.assertIn(
            "No projects match", self.commands.execute(99, "project status missing --all")
        )
        self.assertEqual(self.client.mock_calls, [])

    def test_invalid_query_options_do_not_show_projects_or_change_settings(self):
        project = self.create()
        for command in (
            "list research",
            "list --unknown",
            "list --all --all",
            "list --all research",
            "status --unknown",
            "status --all --all",
            "status research --unknown",
            "status research second --all",
            "sync research --all",
            "auth research --all",
            "pause research --all",
            "remove research --all",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.commands.execute(10, "project " + command), USAGE)
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.worker.mock_calls, [])
        self.assertEqual(self.store.get_owned(10, "research"), project)

    def test_creator_names_are_current_and_escaped_along_with_channels(self):
        self.create()
        self.store.change_owned(10, "research", channel="@**all** [Research](https://example.com)")
        self.commands.execute(11, "project list --all")
        self.client.get_members.return_value["members"][0]["full_name"] = "Alice @**all** [team]"
        reply = self.commands.execute(11, "project list --all")
        self.assertIn("Creator: Alice @\u200b\\*\\*all\\*\\* \\[team\\]", reply)
        self.assertIn("Channel: @\u200b\\*\\*all\\*\\* \\[Research\\]", reply)
        self.assertNotIn("@**all**", reply)

    def test_unavailable_creator_names_still_show_projects_and_stable_user_ids(self):
        self.create()
        for response in (
            {"result": "success", "members": []},
            {"result": "error", "msg": "private upstream details"},
        ):
            self.client.get_members.return_value = response
            reply = self.commands.execute(11, "project list --all")
            self.assertIn("**research**", reply)
            self.assertIn("Creator: Unknown user (Zulip user ID 10)", reply)
            self.assertNotIn("private upstream details", reply)
        self.client.get_members.side_effect = requests.Timeout("private upstream details")
        reply = self.commands.execute(11, "project status research --all")
        self.assertIn("temporarily unavailable", reply)
        self.assertIn("Zulip user ID 10", reply)
        self.assertNotIn("private upstream details", reply)

    def test_channel_access_is_required_for_owner_and_bot(self):
        for members in ([1], [10], []):
            self.client.call_endpoint.return_value["subscribers"] = members
            reply = self.commands.execute(
                10, "project setup test https://github.com/users/alice/projects/1 Research"
            )
            self.assertIn("active members", reply)
        self.assertEqual(self.store.list_owned(10), [])

    def test_update_pause_resume_remove_and_sync_only_touch_owned_project(self):
        self.authorize(self.create())
        other = self.authorize(self.create(user=11, channel=30), "P_2")
        ticket = self.store.issue_ticket(11, "research")
        other = self.store.get_owned(11, "research")
        self.store.connection.execute("UPDATE projects SET requested = 0 WHERE id = ?", (other.id,))
        self.commands.execute(10, "project channel research Research")
        self.assertIn("/authorize#", self.commands.execute(10, "project auth research"))
        self.commands.execute(10, "project interval research 60")
        self.assertEqual(self.store.get_owned(10, "research").interval_seconds, 60)
        self.commands.execute(10, "project pause research")
        self.assertFalse(self.store.get_owned(10, "research").enabled)
        self.assertIn("paused", self.commands.execute(10, "project sync research"))
        self.commands.execute(10, "project resume research")
        self.assertIn("requested", self.commands.execute(10, "project sync research"))
        self.commands.execute(10, "project remove research")
        self.assertEqual(self.store.list_owned(10), [])
        self.assertEqual(self.store.get_owned(11, "research"), other)
        self.assertEqual(self.store.ticket_project(ticket), other)
        self.assertEqual(self.store.token_for(other), "github-secret-token")
        requested = self.store.connection.execute(
            "SELECT requested FROM projects WHERE id = ?", (other.id,)
        ).fetchone()[0]
        self.assertEqual(requested, 0)

    def test_invalid_commands_and_tokens_are_not_echoed(self):
        self.create()
        for command in (
            'project setup "',
            "project interval research 0",
            "project interval research secret-token",
            "project auth research ghp_secret",
        ):
            reply = self.commands.execute(10, command)
            self.assertNotIn("secret-token", reply)
            self.assertNotIn("ghp_secret", reply)

    def test_project_url_and_public_origin_validation(self):
        self.assertEqual(
            parse_project_url("https://github.com/orgs/team/projects/7")[1], "organization"
        )
        for url in (
            "http://github.com/users/x/projects/1",
            "https://evil.test/users/x/projects/1",
            "https://github.com@evil.test/users/x/projects/1",
            "https://github.com/users/x/projects/0",
        ):
            with self.assertRaises(ProjectError):
                parse_project_url(url)
        for url in (
            "http://example.com",
            "https://x/a",
            "https://x/?secret",
            "https://user:pass@x",
        ):
            with self.assertRaises(ValueError):
                auth_origin(url)
        self.assertEqual(auth_origin("http://localhost:8765/"), "http://localhost:8765")
        self.assertEqual(auth_origin("https://AUTH.example.com:443/"), "https://auth.example.com")
        with self.assertRaises(ValueError):
            auth_origin("https://example.com:bad")
        for number in ("9" * 5000, "2147483648"):
            reply = self.commands.execute(
                10, f"project setup test https://github.com/users/alice/projects/{number} Research"
            )
            self.assertEqual(self.store.list_owned(10), [])
            self.assertIsInstance(reply, str)


class WorkerTests(ProjectFixture):
    def test_failure_is_isolated_and_later_manual_retry_works(self):
        first = self.authorize(self.create())
        second = self.authorize(self.create(user=11, channel=30), "P_2")
        completed = Event()
        failed = Event()
        retried = Event()
        calls = []

        def sync(project, stop):
            calls.append(project.id)
            if project.id == first.id and calls.count(first.id) == 1:
                failed.set()
                raise RuntimeError("secret-token")
            if project.id == second.id:
                completed.set()
            if calls.count(first.id) == 2:
                retried.set()
            return SyncResult(tasks=1)

        worker = SyncWorker(self.store, sync)
        with self.assertLogs("astrochat.sync_worker", "ERROR") as logs:
            worker.thread.start()
            try:
                self.assertTrue(completed.wait(5))
                self.assertTrue(failed.wait(5))
                self.store.request_owned(10, "research")
                worker.wake.set()
                self.assertTrue(retried.wait(5))
            finally:
                worker.close()
        self.assertNotIn("secret-token", "".join(logs.output))
        self.assertFalse(worker.thread.is_alive())

    def test_pause_cancels_active_job_without_overwriting_new_state(self):
        project = self.authorize(self.create())
        started = Event()
        cancelled = Event()

        def sync(project, stop):
            started.set()
            if stop.wait(5):
                cancelled.set()
            return SyncResult(tasks=1)

        worker = SyncWorker(self.store, sync)
        worker.thread.start()
        try:
            self.assertTrue(started.wait(5))
            changed = self.store.change_owned(10, "research", enabled=False)
            worker.changed(project.id)
            self.assertTrue(cancelled.wait(5))
        finally:
            worker.close()
        self.assertEqual(self.store.get_owned(10, "research"), changed)

    def test_idle_worker_closes_promptly(self):
        worker = SyncWorker(self.store, Mock())
        worker.thread.start()
        worker.close()
        self.assertFalse(worker.thread.is_alive())

    def test_runner_rechecks_membership_before_reading_github(self):
        project = self.authorize(self.create())
        client = Mock()
        client.call_endpoint.return_value = {"result": "success", "subscribers": [1]}
        runner = SyncRunner(self.store, self.path, client, 1)
        with patch("astrochat.project_commands.GitHubClient") as github:
            with self.assertRaises(ProjectError):
                runner(project, Event())
            github.assert_not_called()

    def test_runner_refreshes_shared_account_snapshot_for_each_project_and_poll(self):
        first = self.authorize(self.create())
        second = self.authorize(self.create(user=11, channel=30), "P_2")
        client = Mock()
        client.call_endpoint.return_value = {"result": "success", "subscribers": [1, 10, 11]}
        runner = SyncRunner(self.store, self.path, client, 1)
        self.store.set_github_account(10, "Alice", 11)
        with (
            patch("astrochat.project_commands.GitHubClient"),
            patch("astrochat.project_commands.ProjectSync") as sync,
        ):
            for project in (first, second):
                runner(project, Event())
                self.assertEqual(sync.call_args.args[4], {"alice": 10})
            self.store.set_github_account(10, None, 11)
            self.store.set_github_account(11, "Alice", 10)
            runner(first, Event())
            self.assertEqual(sync.call_args.args[4], {"alice": 11})
            self.store.set_github_account(11, None, 10)
            runner(first, Event())
            self.assertEqual(sync.call_args.args[4], {})
        client.get_members.assert_not_called()

    def test_runner_namespaces_state_and_restores_the_same_channel_mapping(self):
        first = self.authorize(self.create())
        second = self.authorize(self.create(user=11, channel=30), "P_1")
        client = Mock(base_url="https://zulip/", email="bot")
        client.call_endpoint.return_value = {"result": "success", "subscribers": [1, 10, 11]}
        runner = SyncRunner(self.store, self.path, client, 1)
        with patch("astrochat.project_commands.GitHubClient") as github:
            github.return_value.project.return_value = {"id": "P_1"}
            github.return_value.tasks.return_value = ([], 0)
            for project in (first, second):
                self.assertEqual(runner(project, Event()).failed, 0)
            states = sorted((self.path / "states").glob("*.sqlite3"))
            self.assertEqual(len(states), 2)
            changed = self.store.change_owned(10, "research", channel_id=40)
            runner(changed, Event())
            original = self.store.change_owned(10, "research", channel_id=20)
            runner(original, Event())
            self.assertEqual(len(list((self.path / "states").glob("*.sqlite3"))), 3)
