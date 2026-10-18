import argparse
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from zulip_bots.finder import import_module_from_source

from astrochat.__main__ import ChatEvents, run
from astrochat.astrochat import AstroChatBot


class BotTests(unittest.TestCase):
    def setUp(self):
        self.handler = Mock(user_id=1, email="bot@example.com", full_name="AstroChat")
        self.projects = Mock()
        self.bot = AstroChatBot(self.projects)
        self.events = ChatEvents(self.bot, self.handler)

    def event(self, content, flags=None, **changes):
        return {
            "type": "message",
            "flags": flags or [],
            "message": {
                "id": 100,
                "sender_id": 2,
                "content": content,
                "type": "stream",
                "display_recipient": "research",
                "subject": "task",
                **changes,
            },
        }

    def test_channel_mention_never_exposes_configuration_or_authorization_link(self):
        self.events(self.event("@**AstroChat** project auth research", ["mentioned"]))
        self.projects.execute.assert_not_called()
        self.assertIn("one-to-one DM", self.handler.send_reply.call_args.args[1])

    def test_plain_channel_messages_and_self_messages_are_ignored(self):
        self.events(self.event("project sync"))
        self.events(self.event("@**AstroChat** project sync", ["mentioned"], sender_id=1))
        self.events(self.event("hello @**AstroChat** project sync", ["mentioned"]))
        self.projects.execute.assert_not_called()
        self.handler.send_reply.assert_not_called()

    def test_direct_message_returns_status(self):
        self.projects.execute.return_value = "tasks=3"
        self.events(
            self.event(
                "project status",
                type="private",
                display_recipient=[
                    {"id": 1, "email": "bot@example.com"},
                    {"id": 2, "email": "alice@example.com"},
                ],
            )
        )
        self.assertEqual(self.handler.send_reply.call_args.args[1], "tasks=3")
        self.projects.execute.assert_called_once_with(2, "project status")

    def test_group_dm_cannot_manage_projects_even_when_mentioned(self):
        self.events(
            self.event(
                "@**AstroChat** project list",
                ["mentioned"],
                type="private",
                display_recipient=[
                    {"id": 1, "email": "bot@example.com"},
                    {"id": 2, "email": "alice@example.com"},
                    {"id": 3, "email": "bob@example.com"},
                ],
            )
        )
        self.projects.execute.assert_not_called()

    def test_private_command_preserves_case_and_uses_message_sender(self):
        self.bot.handle_message(
            {
                "content": 'project setup Mine https://github.com/users/Alice/projects/1 "My Channel"',
                "sender_id": 42,
                "type": "private",
                "display_recipient": [{"id": 1}, {"id": 42}],
            },
            self.handler,
        )
        args = self.projects.execute.call_args.args
        self.assertEqual(args[0], 42)
        self.assertIn("Alice", args[1])
        self.assertIn("My Channel", args[1])

    def test_legacy_bot_without_config_still_works(self):
        module = import_module_from_source("astrochat/astrochat.py", "astrochat_test_plugin")
        bot = module.handler_class()
        bot.handle_message(
            {
                "content": "project sync",
                "sender_id": 2,
                "type": "private",
                "display_recipient": [{"id": 1}, {"id": 2}],
            },
            self.handler,
        )
        self.assertIn("not configured", self.handler.send_reply.call_args.args[1])
        bot.handle_message({"content": "hello", "sender_id": 2}, self.handler)
        self.handler.react.assert_called_once()


class CliTests(unittest.TestCase):
    def test_interrupt_closes_authorization_before_worker_and_database(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                data_dir=Path(directory),
                zulip_config="zuliprc",
                once=False,
                dry_run=False,
                auth_url="https://astrochat.example.com",
                auth_port=8765,
            )
            closed = []
            with (
                patch("astrochat.__main__.zulip.Client") as client,
                patch("astrochat.__main__.ExternalBotHandler") as handler,
                patch("astrochat.__main__.SyncWorker") as worker,
                patch("astrochat.__main__.AuthorizationServer") as server,
            ):
                client.return_value.base_url = "https://zulip.example.com/api/"
                client.return_value.email = "bot@example.com"
                client.return_value.get_profile.return_value = {"result": "success", "user_id": 1}
                client.return_value.call_on_each_event.side_effect = KeyboardInterrupt
                handler.return_value.user_id = 1
                server.return_value.close.side_effect = lambda: closed.append("server")
                worker.return_value.close.side_effect = lambda: closed.append("worker")
                with self.assertRaises(KeyboardInterrupt):
                    run(args)
                self.assertEqual(closed, ["server", "worker"])
                self.assertEqual(client.return_value.session.close.call_count, 3)
                connection = worker.call_args.args[0].connection
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_once_returns_failure_exit_code_and_does_not_start_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                data_dir=Path(directory),
                zulip_config="zuliprc",
                once=True,
                dry_run=False,
                auth_url="https://astrochat.example.com",
                auth_port=8765,
            )
            with (
                patch("astrochat.__main__.zulip.Client") as client,
                patch("astrochat.__main__.SyncWorker") as worker,
                patch("astrochat.__main__.AuthorizationServer") as server,
            ):
                client.return_value.base_url = "https://zulip.example.com/api/"
                client.return_value.email = "bot@example.com"
                client.return_value.get_profile.return_value = {"result": "success", "user_id": 1}
                worker.return_value.run_saved_once.return_value = 1
                self.assertEqual(run(args), 1)
                worker.return_value.run_saved_once.assert_called_once_with(dry_run=False)
                worker.return_value.thread.start.assert_not_called()
                server.assert_not_called()
                client.return_value.call_on_each_event.assert_not_called()
                self.assertEqual(client.return_value.session.close.call_count, 2)
