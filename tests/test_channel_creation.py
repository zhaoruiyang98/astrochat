import re
from unittest.mock import Mock, patch

import requests

from astrochat.project_commands import ProjectCommands
from astrochat.project_store import ProjectError
from test_project_management import ProjectFixture


class ChannelCreationTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.client = Mock(feature_level=417)
        self.client.get_stream_id.side_effect = self.missing_channel
        self.client.call_endpoint.side_effect = self.endpoint
        self.worker = Mock()
        self.commands = self.new_commands()

    def new_commands(self):
        return ProjectCommands(self.store, self.worker, self.client, 1, "https://auth.example.com")

    @staticmethod
    def missing_channel(channel):
        return {
            "result": "error",
            "code": "BAD_REQUEST",
            "msg": f"Invalid channel name '{channel}'",
        }

    @staticmethod
    def endpoint(url, method, request=None):
        if url == "channels/create" and method == "POST":
            return {"result": "success", "id": 31}
        if url == "streams/31/members" and method == "GET":
            return {"result": "success", "subscribers": [1, 10, 11]}
        raise AssertionError(f"Unexpected request: {method} {url}")

    def prompt(self, user=10, command=None):
        command = command or (
            'project setup Research https://github.com/users/Alice/projects/2 "My Channel" 300'
        )
        reply = self.commands.execute(user, command)
        self.assertIn("**public channel**", reply)
        self.assertIn("10 minutes", reply)
        self.assertIn("Anonymous web access will be disabled", reply)
        code = re.search(r"`project confirm ([a-f0-9]+)`", reply).group(1)
        self.assertIn(f"`project cancel {code}`", reply)
        return code

    def test_setup_waits_for_confirmation_then_creates_public_channel_and_authorization(self):
        code = self.prompt()
        self.assertEqual(self.store.list_owned(10), [])
        self.client.call_endpoint.assert_not_called()
        self.worker.changed.assert_not_called()

        reply = self.commands.execute(10, f"project confirm {code}")
        self.assertIn("Created public channel **My Channel**", reply)
        self.assertIn("/authorize#", reply)
        project = self.store.get_owned(10, "research")
        self.assertEqual(
            (project.owner, project.number, project.channel, project.channel_id),
            ("Alice", 2, "My Channel", 31),
        )
        self.assertEqual(project.interval_seconds, 300)
        self.assertFalse(project.authorized)
        request = self.client.call_endpoint.call_args_list[0]
        self.assertEqual(request.args, ("channels/create",))
        self.assertEqual(request.kwargs["method"], "POST")
        options = request.kwargs["request"]
        self.assertEqual(options["name"], "My Channel")
        self.assertEqual(options["subscribers"], [10, 1])
        self.assertFalse(options["invite_only"])
        self.assertFalse(options["is_web_public"])
        self.assertEqual(options["can_administer_channel_group"]["direct_members"], [10, 1])
        self.worker.changed.assert_called_once_with(project.id)

        self.client.reset_mock()
        self.assertIn("No matching", self.commands.execute(10, f"project confirm {code}"))
        self.assertEqual(self.client.mock_calls, [])

    def test_change_keeps_original_settings_until_confirmation_and_preserves_token(self):
        self.authorize(self.create())
        ticket = self.store.issue_ticket(10, "research")
        original = self.store.get_owned(10, "research")
        other = self.create(user=11)
        code = self.prompt(command='project channel research "New Channel"')
        self.assertEqual(self.store.get_owned(10, "research"), original)
        self.assertEqual(self.store.ticket_project(ticket), original)
        self.assertEqual(self.worker.mock_calls, [])

        self.assertIn("Settings updated", self.commands.execute(10, f"project confirm {code}"))
        updated = self.store.get_owned(10, "research")
        self.assertEqual((updated.channel, updated.channel_id), ("New Channel", 31))
        self.assertEqual(updated.id, original.id)
        self.assertEqual(self.store.token_for(updated), "github-secret-token")
        self.assertEqual(self.store.get_owned(11, "research"), other)
        with self.assertRaises(ProjectError):
            self.store.ticket_project(ticket)
        self.worker.changed.assert_called_once_with(original.id)

    def test_cancel_is_one_use_and_does_not_create_or_save(self):
        code = self.prompt()
        self.assertIn("cancelled", self.commands.execute(10, f"project cancel {code}"))
        self.assertIn("No matching", self.commands.execute(10, f"project confirm {code}"))
        self.client.call_endpoint.assert_not_called()
        self.assertEqual(self.store.list_owned(10), [])

    def test_other_user_and_wrong_codes_cannot_confirm_or_cancel(self):
        alice = self.prompt()
        bob = self.prompt(user=11)
        for user, code in ((11, alice), (10, bob), (10, "wrong-code")):
            for action in ("confirm", "cancel"):
                self.assertIn(
                    "No matching", self.commands.execute(user, f"project {action} {code}")
                )
        self.client.call_endpoint.assert_not_called()
        self.assertIn("/authorize#", self.commands.execute(10, f"project confirm {alice}"))
        self.assertIn("/authorize#", self.commands.execute(11, f"project confirm {bob}"))
        self.assertEqual(len(self.store.list_projects()), 2)

    def test_confirmation_expires_and_restart_discards_it(self):
        with patch("astrochat.project_commands.time.monotonic", return_value=100):
            code = self.prompt()
        with patch("astrochat.project_commands.time.monotonic", return_value=700):
            self.assertIn("expired", self.commands.execute(10, f"project confirm {code}"))
        code = self.prompt()
        self.commands = self.new_commands()
        self.assertIn("No matching", self.commands.execute(10, f"project confirm {code}"))
        self.client.call_endpoint.assert_not_called()
        self.assertEqual(self.store.list_projects(), [])

    def test_new_prompt_replaces_only_the_same_users_pending_request(self):
        first = self.prompt()
        other = self.prompt(user=11)
        second = self.prompt(
            command="project setup second https://github.com/users/alice/projects/3 Other"
        )
        self.assertIn("No matching", self.commands.execute(10, f"project confirm {first}"))
        self.assertIn("/authorize#", self.commands.execute(10, f"project confirm {second}"))
        self.assertIn("cancelled", self.commands.execute(11, f"project cancel {other}"))
        self.assertEqual([project.name for project in self.store.list_owned(10)], ["second"])

    def test_stale_channel_changes_are_rejected_before_network_requests(self):
        for change in ("pause", "auth", "remove", "recreate"):
            with self.subTest(change=change):
                self.create(name=change)
                code = self.prompt(command=f"project channel {change} New")
                if change == "recreate":
                    self.store.remove_owned(10, change)
                    self.create(name=change)
                else:
                    self.commands.execute(10, f"project {change} {change}")
                self.client.reset_mock()
                reply = self.commands.execute(10, f"project confirm {code}")
                self.assertTrue("settings changed" in reply or "not found" in reply)
                self.assertEqual(self.client.mock_calls, [])

    def test_duplicate_setup_names_do_not_create_unused_channels(self):
        code = self.prompt()
        self.create()
        self.client.reset_mock()
        self.assertIn("already have", self.commands.execute(10, f"project confirm {code}"))
        self.assertIn(
            "already have",
            self.commands.execute(
                10, "project setup research https://github.com/users/alice/projects/1 New"
            ),
        )
        self.assertEqual(self.client.mock_calls, [])

    def test_channel_that_appears_during_confirmation_is_not_modified(self):
        code = self.prompt()
        self.client.get_stream_id.side_effect = None
        self.client.get_stream_id.return_value = {"result": "success", "stream_id": 99}
        reply = self.commands.execute(10, f"project confirm {code}")
        self.assertIn("now exists", reply)
        self.client.call_endpoint.assert_not_called()
        self.assertEqual(self.store.list_projects(), [])

    def test_lookup_errors_do_not_offer_channel_creation(self):
        for error in (
            {"result": "error", "code": "BAD_REQUEST", "msg": "Permission denied"},
            {"result": "error", "code": "UNAUTHORIZED", "msg": "private upstream details"},
            requests.Timeout("private upstream details"),
        ):
            with self.subTest(error=error):
                self.client.get_stream_id.side_effect = [error]
                reply = self.commands.execute(
                    10, "project setup research https://github.com/users/alice/projects/1 New"
                )
                self.assertIn("Could not check", reply)
                self.assertNotIn("project confirm", reply)
                self.assertNotIn("private upstream details", reply)
        self.client.call_endpoint.assert_not_called()
        self.assertEqual(self.store.list_projects(), [])

    def test_creation_failure_keeps_existing_config_and_consumes_confirmation(self):
        original = self.create()
        for response in (
            {"result": "error", "code": "BAD_REQUEST", "msg": "private upstream details"},
            {"result": "error", "code": "CHANNEL_ALREADY_EXISTS", "msg": "already exists"},
            requests.Timeout("private upstream details"),
        ):
            with self.subTest(response=response):
                code = self.prompt(command="project channel research New")
                self.client.call_endpoint.side_effect = [response]
                reply = self.commands.execute(10, f"project confirm {code}")
                self.assertIn("Settings were not saved", reply)
                self.assertNotIn("private upstream details", reply)
                self.assertEqual(self.store.get_owned(10, "research"), original)
                self.assertIn("No matching", self.commands.execute(10, f"project confirm {code}"))
        self.assertEqual(self.worker.mock_calls, [])

    def test_membership_failure_after_creation_reports_channel_was_created(self):
        original = self.create()
        code = self.prompt(command="project channel research New")
        self.client.call_endpoint.side_effect = [
            {"result": "success", "id": 31},
            {"result": "success", "subscribers": [1]},
        ]
        reply = self.commands.execute(10, f"project confirm {code}")
        self.assertIn("Created public channel", reply)
        self.assertIn("Could not save project settings", reply)
        self.assertEqual(self.store.get_owned(10, "research"), original)

    def test_concurrent_authorization_during_creation_does_not_overwrite_settings(self):
        original = self.create()
        code = self.prompt(command="project channel research New")

        def endpoint(url, method, request=None):
            if url == "channels/create":
                self.store.authorize(original, "new-token", "P_1")
            return self.endpoint(url, method, request)

        self.client.call_endpoint.side_effect = endpoint
        reply = self.commands.execute(10, f"project confirm {code}")
        self.assertIn("Created public channel", reply)
        self.assertIn("Project settings changed", reply)
        project = self.store.get_owned(10, "research")
        self.assertEqual(project.channel_id, original.channel_id)
        self.assertEqual(self.store.token_for(project), "new-token")

    def test_older_servers_can_still_use_existing_channels(self):
        self.client.feature_level = 416
        command = "project setup research https://github.com/users/alice/projects/1 Research"
        reply = self.commands.execute(10, command)
        self.assertIn("feature level 417", reply)
        self.assertIn("manually", reply)
        self.client.call_endpoint.assert_not_called()
        self.client.get_stream_id.side_effect = None
        self.client.get_stream_id.return_value = {"result": "success", "stream_id": 31}
        self.assertIn("/authorize#", self.commands.execute(10, command))
        self.client.call_endpoint.assert_called_once_with("streams/31/members", method="GET")

    def test_foreign_project_cannot_start_channel_creation(self):
        self.create()
        self.assertIn("not found", self.commands.execute(11, "project channel research New"))
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.commands.confirmations, {})

    def test_empty_channel_and_invalid_interval_do_not_prompt(self):
        for suffix in ('""', '"   "', "New 0"):
            reply = self.commands.execute(
                10, f"project setup research https://github.com/users/alice/projects/1 {suffix}"
            )
            self.assertNotIn("project confirm", reply)
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.store.list_projects(), [])
