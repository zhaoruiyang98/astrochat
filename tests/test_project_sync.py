import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import requests

from astrochat.image_sync import ImageSyncError
from astrochat.config import SyncConfig
from astrochat.github import Discussion, GitHubError, Task
from astrochat.markdown import split_message
from astrochat.project_sync import ProjectSync, render, topic_name
from astrochat.sync_state import open_state
from astrochat.sync_worker import SyncWorker


class FakeZulip:
    base_url = "https://example.zulipchat.com/api/"
    email = "astrochat-bot@example.zulipchat.com"

    def __init__(self):
        self.messages = {}
        self.next_id = 1
        self.fail = None
        self.calls = []
        self.reads = []

    def get_stream_id(self, channel):
        return {"result": "success", "stream_id": 10}

    def send_message(self, message):
        self.calls.append("send")
        if self.fail == "send":
            return {"result": "error", "msg": "rate limited", "code": "RATE_LIMIT_HIT"}
        identifier = self.next_id
        self.next_id += 1
        self.messages[identifier] = dict(message)
        return {"result": "success", "id": identifier}

    def update_message(self, message):
        action = "rename" if "topic" in message else "update"
        self.calls.append(action)
        if self.fail == action:
            return {"result": "error", "msg": "editing is not allowed"}
        target = self.messages[message["message_id"]]
        if "topic" in message:
            assert message["propagate_mode"] == "change_all"
            channel, topic = target["to"], target["topic"]
            for existing in self.messages.values():
                if (
                    existing.get("to") == channel
                    and existing["topic"].casefold() == topic.casefold()
                ):
                    existing["topic"] = message["topic"]
        if "content" in message:
            target["content"] = message["content"]
        return {"result": "success"}

    def call_endpoint(self, url, method):
        self.reads.append(url)
        assert method == "GET" and url.startswith("messages/")
        message_id = int(url.removeprefix("messages/"))
        if message_id not in self.messages:
            return {"result": "error", "msg": "Invalid message"}
        message = self.messages[message_id]
        return {
            "result": "success",
            "message": {
                "type": message["type"],
                "stream_id": message["to"],
                "subject": message["topic"],
            },
        }

    def get_messages(self, request):
        self.reads.append(request)
        narrow = {term["operator"]: term["operand"] for term in request["narrow"]}
        matches = [
            {"id": message_id}
            for message_id, message in self.messages.items()
            if message.get("to") == narrow["stream"]
            and message["topic"].casefold() == narrow["topic"].casefold()
        ]
        return {"result": "success", "messages": matches[-1:]}

    def delete_message(self, message_id):
        self.calls.append("delete")
        if self.fail == "delete":
            return {"result": "error", "msg": "deletion is not allowed"}
        del self.messages[message_id]
        return {"result": "success"}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.config = SyncConfig("example", 1, "research", self.path)
        self.state = self.resources.enter_context(open_state(self.path))
        self.client = FakeZulip()
        self.github = Mock()
        self.github.project.return_value = {"id": "P_1"}
        self.body = Discussion(
            "I_1",
            "Description",
            "https://github.com/e/r/issues/1",
            "alice",
            "2026-10-18T00:00:00Z",
            "Issue",
        )
        self.comment = replace(self.body, id="C_1", body="Discussion", kind="Comment")
        self.task = Task("I_1", "Issue", "Research", "e/r#1", self.body)
        self.github.tasks.return_value = ([self.task], 0)
        self.github.discussions.return_value = [self.body, self.comment]
        self.service = ProjectSync(self.config, self.github, self.client, self.state)

    def reload(self, dry_run=False):
        self.resources.close()
        self.state = self.resources.enter_context(open_state(self.path, dry_run))
        self.service = ProjectSync(self.config, self.github, self.client, self.state)

    def test_import_restart_and_repeat_do_not_duplicate(self):
        self.assertEqual(self.service.run_once().sent, 2)
        self.reload()
        changes = self.state.connection.total_changes
        result = self.service.run_once()
        self.assertEqual((result.sent, result.updated, result.deleted), (0, 0, 0))
        self.assertEqual(self.state.connection.total_changes, changes)
        self.assertEqual(len(self.client.messages), 2)
        self.assertIn("alice", self.client.messages[1]["content"])

    def test_account_mapping_changes_update_existing_messages_without_new_posts_or_topic_moves(
        self,
    ):
        task = replace(self.task, assignees=("Alice", "bob"))
        self.github.tasks.return_value = ([task], 0)
        self.service.run_once()
        topic = self.client.messages[1]["topic"]
        self.client.messages[999] = {"content": "local reply", "topic": topic}
        for mapping, expected in (
            ({"alice": 10}, 2),
            ({"alice": 10}, 0),
            ({"alice": 11}, 2),
            ({}, 2),
        ):
            with self.subTest(mapping=mapping):
                self.reload()
                self.service = ProjectSync(
                    self.config, self.github, self.client, self.state, mapping
                )
                result = self.service.run_once()
                self.assertEqual((result.sent, result.updated, result.renamed), (0, expected, 0))
                self.assertEqual(set(self.client.messages), {1, 2, 999})
                self.assertEqual(self.client.messages[999]["content"], "local reply")
                self.assertEqual(self.client.messages[1]["topic"], topic)
                for message_id in (1, 2):
                    content = self.client.messages[message_id]["content"]
                    self.assertNotIn("@**", content)
                    if mapping:
                        self.assertIn(f"@_**|{mapping['alice']}**", content)
                        self.assertNotIn("alice", content.lower())
                    else:
                        self.assertNotIn("@_**", content)
                if mapping:
                    self.assertIn(
                        f"Assignees: @_**|{mapping['alice']}**, bob",
                        self.client.messages[1]["content"],
                    )

    def test_author_and_assignment_mappings_are_silent_and_do_not_rewrite_body_mentions(self):
        task = replace(self.task, assignees=("ALICE", "unknown"))
        accounts = {"alice": 10, "bob": 11}
        for kind in ("Issue", "PullRequest", "DraftIssue", "Comment", "Review", "Review comment"):
            entry = replace(
                self.body,
                kind=kind,
                body="GitHub @alice and @**all** and @_**|11** remain source text.",
            )
            content = render(task, entry, github_users=accounts)
            self.assertIn(f"**{kind}** · @_**|10** ·", content)
            self.assertIn("Assignees: @_**|10**, unknown", content)
            self.assertNotIn("alice", content.split("[View on GitHub]")[0].lower())
            self.assertIn("GitHub @alice", content)
            self.assertNotIn("@**", content)
            self.assertNotIn("@_**|11**", content)
        for action in ("Assigned to", "Unassigned from"):
            event = replace(
                self.comment, is_event=True, assignment=(action, "bob"), body=f"{action} bob"
            )
            content = render(task, event, github_users=accounts)
            self.assertTrue(content.endswith(f"{action} @_**|11**"))
            self.assertNotIn("@**", content)
        event = replace(
            self.comment, is_event=True, body="", status_change=("Backlog", "In progress")
        )
        self.assertIn("**Comment** · @_**|10** ·", render(task, event, github_users=accounts))
        self.assertTrue(
            render(task, event, github_users=accounts).endswith("**Backlog** → **In progress**")
        )

    def test_assignment_event_mapping_refreshes_on_next_poll(self):
        event = replace(
            self.comment,
            id="E_1",
            is_event=True,
            body="Assigned to bob",
            assignment=("Assigned to", "bob"),
        )
        self.github.discussions.return_value = [self.body, event]
        self.service.run_once()
        self.service = ProjectSync(self.config, self.github, self.client, self.state, {"bob": 11})
        result = self.service.run_once()
        self.assertEqual((result.sent, result.updated), (0, 1))
        self.assertTrue(self.client.messages[2]["content"].endswith("Assigned to @_**|11**"))
        self.assertEqual(self.service.run_once().updated, 0)

    def test_draft_assignment_observation_uses_silent_labels_but_does_not_invent_actor(self):
        draft = replace(self.task, type="DraftIssue", assignees=("alice",))
        self.github.tasks.return_value = ([draft], 0)
        self.github.discussions.return_value = [self.body]
        self.service = ProjectSync(
            self.config,
            self.github,
            self.client,
            self.state,
            {"alice": 10, "bob": 11, "astrochat": 12},
        )
        self.service.run_once()
        self.github.tasks.return_value = ([replace(draft, assignees=("bob",))], 0)
        self.assertEqual(self.service.run_once().sent, 1)
        notification = self.client.messages[2]["content"]
        self.assertIn("Assignees: @_**|10** → @_**|11**", notification)
        self.assertNotIn("alice", notification)
        self.assertNotIn("bob", notification)
        self.assertNotIn("|12", notification)
        self.assertNotIn("@**", notification)
        self.assertEqual(self.service.run_once().sent, 0)

    def test_account_mapping_preview_does_not_edit_messages_or_state(self):
        self.service.run_once()
        original = {key: dict(value) for key, value in self.client.messages.items()}
        before = self.path.read_bytes()
        self.reload(dry_run=True)
        self.service = ProjectSync(self.config, self.github, self.client, self.state, {"alice": 10})
        result = self.service.run_once()
        self.assertEqual((result.sent, result.updated), (0, 2))
        self.assertEqual(self.client.messages, original)
        self.assertEqual(self.path.read_bytes(), before)

    def test_project_fields_update_only_the_first_message_and_preserve_ids_after_restart(self):
        self.service.run_once()
        self.client.messages[999] = {"content": "local reply", "topic": topic_name(self.task)}
        comment = dict(self.client.messages[2])
        snapshots = (
            (("Priority", "High"), ("Start date", "2026-10-18"), ("Target date", None)),
            (("Priority", "Medium"), ("Start date", "2026-10-19"), ("Target date", "2026-10-31")),
            (("Priority", None), ("Start date", None), ("Target date", None)),
            (),
        )
        for fields in snapshots:
            with self.subTest(fields=fields):
                self.github.tasks.return_value = ([replace(self.task, project_fields=fields)], 0)
                result = self.service.run_once()
                self.assertEqual(
                    (result.sent, result.updated, result.deleted, result.renamed), (0, 1, 0, 0)
                )
                content = self.client.messages[1]["content"]
                for name, value in fields:
                    self.assertIn(f"{name}: {value or '(unset)'}", content)
                if not fields:
                    for name in ("Priority", "Start date", "Target date"):
                        self.assertNotIn(f"{name}:", content)
                self.assertEqual(set(self.client.messages), {1, 2, 999})
                self.assertEqual(self.client.messages[2], comment)
                self.assertEqual(self.client.messages[999]["content"], "local reply")
                self.reload()
                self.assertEqual(self.service.run_once().updated, 0)

    def test_project_fields_are_in_the_first_part_of_a_long_description_and_are_literal(self):
        task = replace(
            self.task,
            project_fields=(
                ("Priority", "@**all** [urgent]"),
                ("Start date", "2026-10-18"),
                ("Target date", None),
            ),
        )
        body = replace(self.body, body="long description " * 2000)
        self.github.tasks.return_value = ([task], 0)
        self.github.discussions.return_value = [body, self.comment]
        self.assertGreater(self.service.run_once().sent, 2)
        first = self.client.messages[1]["content"]
        self.assertIn("Start date: 2026-10-18", first)
        self.assertIn("Target date: (unset)", first)
        self.assertNotIn("@**all**", first)
        self.assertIn("&#91;urgent&#93;", first)
        self.assertTrue(
            all(len(message["content"]) <= 10000 for message in self.client.messages.values())
        )

    def test_project_field_edit_failure_preserves_checkpoint_and_retries(self):
        self.service.run_once()
        before = self.path.read_bytes()
        original = self.client.messages[1]["content"]
        self.github.tasks.return_value = (
            [replace(self.task, project_fields=(("Priority", "High"),))],
            0,
        )
        self.client.fail = "update"
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            self.assertEqual(self.service.run_once().failed, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.messages[1]["content"], original)
        self.reload()
        self.client.fail = None
        self.assertEqual(self.service.run_once().updated, 1)
        self.assertIn("Priority: High", self.client.messages[1]["content"])

    def test_project_field_preview_does_not_change_messages_or_checkpoints(self):
        self.service.run_once()
        before = self.path.read_bytes()
        original = {key: dict(value) for key, value in self.client.messages.items()}
        self.github.tasks.return_value = (
            [replace(self.task, project_fields=(("Target date", "2026-11-01"),))],
            0,
        )
        self.reload(dry_run=True)
        result = self.service.run_once()
        self.assertEqual((result.updated, result.sent, result.deleted), (1, 0, 0))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.messages, original)

    def test_status_and_assignee_history_imports_once_and_preserves_local_replies(self):
        self.service.run_once()
        self.client.messages[999] = {
            "type": "stream",
            "to": 10,
            "topic": topic_name(self.task),
            "content": "local reply",
        }
        task = replace(self.task, status="Done", assignees=("alice", "bob"))
        events = [
            replace(
                self.comment,
                id="E_1",
                kind="Project status",
                body="",
                is_event=True,
                status_change=("(unset)", "Backlog"),
            ),
            replace(
                self.comment,
                id="E_2",
                kind="Project status",
                body="",
                is_event=True,
                status_change=("Backlog", "In progress"),
            ),
            replace(
                self.comment,
                id="E_3",
                kind="Project status",
                body="",
                is_event=True,
                status_change=("In progress", "Done"),
            ),
            replace(
                self.comment, id="E_4", kind="Assignment", body="Assigned to bob", is_event=True
            ),
        ]
        self.github.tasks.return_value = ([task], 0)
        self.github.discussions.return_value = [self.body, self.comment, *events]
        result = self.service.run_once()
        self.assertEqual((result.sent, result.updated, result.renamed), (4, 1, 1))
        self.assertIn("Project status: Done", self.client.messages[1]["content"])
        self.assertIn("Assignees: alice, bob", self.client.messages[1]["content"])
        self.assertTrue(self.client.messages[5]["content"].endswith("**In progress** → **Done**"))
        self.assertTrue(
            all(message["topic"] == topic_name(task) for message in self.client.messages.values())
        )
        self.assertEqual(self.client.messages[999]["content"], "local reply")
        self.reload()
        result = self.service.run_once()
        self.assertEqual((result.sent, result.updated, result.renamed), (0, 0, 0))
        self.github.discussions.return_value = [self.body]
        self.assertEqual(self.service.run_once().deleted, 1)
        self.assertTrue(
            all(f"event:{event.id}:0" in self.state.messages_for(task.id) for event in events)
        )

    def test_done_resolves_and_leaving_done_unresolves_whole_topic(self):
        self.service.run_once()
        original = self.client.messages[1]["topic"]
        self.client.messages[999] = {
            "type": "stream",
            "to": 10,
            "topic": original,
            "content": "reply",
        }
        done = replace(self.task, status="Done")
        self.github.tasks.return_value = ([done], 0)
        self.assertEqual(self.service.run_once().renamed, 1)
        self.assertEqual(self.client.messages[999]["topic"], "✔ " + original)
        self.github.tasks.return_value = ([replace(done, status="In progress")], 0)
        self.assertEqual(self.service.run_once().renamed, 1)
        self.assertEqual(self.client.messages[999]["topic"], original)
        self.assertEqual(set(self.client.messages), {1, 2, 999})

    def test_initial_done_and_title_changes_keep_resolved_prefix_hash_and_length(self):
        done = replace(self.task, status="Done", title="very long title " * 10)
        self.github.tasks.return_value = ([done], 0)
        self.assertEqual(self.service.run_once().sent, 2)
        original = self.client.messages[1]["topic"]
        self.assertTrue(original.startswith("✔ "))
        self.assertLessEqual(len(original), 60)
        changed = replace(done, title="Renamed task")
        self.github.tasks.return_value = ([changed], 0)
        self.assertEqual(self.service.run_once().renamed, 1)
        self.assertTrue(self.client.messages[1]["topic"].startswith("✔ Renamed task"))
        self.assertEqual(original.rsplit(" · ", 1)[1], topic_name(changed).rsplit(" · ", 1)[1])
        for status in (None, "Backlog", "Not Done"):
            self.assertFalse(topic_name(replace(done, status=status)).startswith("✔ "))

    def test_failed_resolution_keeps_checkpoint_and_retries_before_sending_events(self):
        self.service.run_once()
        before = self.path.read_bytes()
        done = replace(self.task, status="Done")
        event = replace(self.comment, id="E_1", body="In progress → Done", is_event=True)
        self.github.tasks.return_value = ([done], 0)
        self.github.discussions.return_value = [self.body, self.comment, event]
        self.client.fail = "rename"
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            self.assertEqual(self.service.run_once().failed, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.reload()
        self.client.fail = None
        result = self.service.run_once()
        self.assertEqual((result.renamed, result.sent, result.failed), (1, 1, 0))

    def test_event_retry_after_partial_import_does_not_duplicate_saved_events(self):
        self.service.run_once()
        events = [replace(self.comment, id=f"E_{index}", is_event=True) for index in (1, 2)]
        self.github.discussions.return_value = [self.body, self.comment, *events]
        original_send = self.client.send_message

        def fail_second(message):
            if len(self.client.messages) == 3:
                return {"result": "error", "msg": "offline"}
            return original_send(message)

        with patch.object(self.client, "send_message", side_effect=fail_second):
            with self.assertLogs("astrochat.project_sync", level="ERROR"):
                self.assertEqual(self.service.run_once().failed, 1)
        self.reload()
        self.assertEqual(self.service.run_once().sent, 1)
        self.assertEqual(len(self.client.messages), 4)

    def test_activity_and_resolution_preview_leave_messages_and_disk_unchanged(self):
        self.service.run_once()
        before = self.path.read_bytes()
        original = {key: dict(value) for key, value in self.client.messages.items()}
        self.github.tasks.return_value = (
            [replace(self.task, status="Done", assignees=("bob",))],
            0,
        )
        self.github.discussions.return_value = [
            self.body,
            self.comment,
            replace(self.comment, id="E_1", is_event=True),
        ]
        self.reload(dry_run=True)
        result = self.service.run_once()
        self.assertEqual((result.renamed, result.sent, result.updated), (1, 1, 1))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.messages, original)

    def test_event_and_current_metadata_are_literal_and_cannot_trigger_mentions(self):
        task = replace(self.task, status="@**all**", assignees=("alice",))
        event = replace(
            self.comment,
            body="Assigned to @**all** ![image](https://github.com/user-attachments/assets/x)",
            is_event=True,
            status_change=("@**all**", "Done → review [again]"),
        )
        for entry in (self.body, event):
            content = render(task, entry)
            self.assertNotIn("@**all**", content)
            self.assertNotIn("![image]", content)
        self.assertIn("**Done → review &#91;again&#93;**", render(task, event))

    def test_draft_polling_tracks_changes_after_baseline_and_keeps_history_on_restart(self):
        draft = replace(self.task, type="DraftIssue", status="Backlog", assignees=("alice",))
        self.github.tasks.return_value = ([draft], 0)
        self.github.discussions.return_value = [self.body]
        self.assertEqual(self.service.run_once().sent, 1)
        self.reload()
        changed = replace(draft, status="In progress", assignees=("bob",))
        self.github.tasks.return_value = ([changed], 0)
        self.assertEqual(self.service.run_once().sent, 1)
        content = self.client.messages[2]["content"]
        self.assertIn("**Backlog** → **In progress**", content)
        self.assertIn("Assignees: alice → bob", content)
        self.assertIn("polling time; actor unavailable", content)
        self.github.tasks.return_value = ([replace(changed, status="Done", assignees=())], 0)
        result = self.service.run_once()
        self.assertEqual((result.sent, result.renamed), (1, 1))
        self.assertIn("Assignees: bob → (unassigned)", self.client.messages[3]["content"])
        self.reload()
        self.assertEqual(self.service.run_once().sent, 0)
        self.assertEqual(len(self.client.messages), 3)
        self.assertEqual(self.state.observation_for(draft.id).sequence, 2)

    def test_failed_draft_notification_does_not_advance_baseline(self):
        draft = replace(self.task, type="DraftIssue", status="Backlog")
        self.github.tasks.return_value = ([draft], 0)
        self.github.discussions.return_value = [self.body]
        self.service.run_once()
        self.github.tasks.return_value = ([replace(draft, status="Done")], 0)
        self.client.fail = "send"
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            self.assertEqual(self.service.run_once().failed, 1)
        self.assertEqual(self.state.observation_for(draft.id).status, "Backlog")
        self.reload()
        self.client.fail = None
        self.assertEqual(self.service.run_once().sent, 1)
        self.assertEqual(self.state.observation_for(draft.id).status, "Done")

    def test_new_edit_delete_preserve_ids_and_do_not_touch_local_replies(self):
        self.service.run_once()
        original_topic = self.client.messages[1]["topic"]
        self.client.messages[999] = {"content": "a local human reply", "topic": original_topic}
        self.github.discussions.return_value = [self.body, replace(self.comment, body="Edited")]
        self.assertEqual(self.service.run_once().updated, 1)
        self.assertIn("Edited", self.client.messages[2]["content"])
        self.github.discussions.return_value = [self.body, replace(self.comment, id="C_2")]
        result = self.service.run_once()
        self.assertEqual((result.sent, result.deleted), (1, 1))
        self.assertNotIn(2, self.client.messages)
        self.assertIn(999, self.client.messages)

    def test_title_edit_renames_whole_topic_and_keeps_local_replies_and_message_ids(self):
        self.service.run_once()
        topic = self.client.messages[1]["topic"]
        local = {"type": "stream", "to": 10, "topic": topic, "content": "a local reply"}
        self.client.messages[999] = dict(local)
        self.client.messages[1000] = {**local, "to": 20}
        self.client.messages[1001] = {**local, "topic": "Other discussion"}
        changed = replace(self.task, title="New title")
        self.github.tasks.return_value = ([changed], 0)
        self.github.discussions.return_value = [
            self.body,
            self.comment,
            replace(self.comment, id="C_2"),
        ]
        result = self.service.run_once()
        self.assertEqual((result.updated, result.renamed, result.sent), (1, 1, 1))
        desired = topic_name(changed)
        for message_id in (1, 2, 3, 999):
            self.assertEqual(self.client.messages[message_id]["topic"], desired)
        self.assertEqual(self.client.messages[999]["content"], "a local reply")
        self.assertEqual(self.client.messages[1000]["topic"], topic)
        self.assertEqual(self.client.messages[1001]["topic"], "Other discussion")
        self.assertEqual(topic.rsplit(" · ", 1)[1], desired.rsplit(" · ", 1)[1])
        self.assertIn("New title", self.client.messages[1]["content"])
        self.reload()
        self.assertEqual(self.state.topic_for(self.task.id, "unused"), desired)
        self.client.calls.clear()
        self.client.reads.clear()
        self.assertEqual(self.service.run_once().renamed, 0)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.reads, [])

    def test_ten_digit_hash_is_shortened_after_restart_preserving_the_discussion(self):
        self.config = replace(self.config, project_id="P_1")
        self.reload()
        task = replace(self.task, title="x" * 80, status="Done")
        self.github.tasks.return_value = ([task], 0)
        old_topic = "✔ " + "x" * 45 + " · f76b88434a"
        with patch("astrochat.project_sync.topic_name", return_value=old_topic):
            self.assertEqual(self.service.run_once().sent, 2)
        self.client.messages[999] = {
            "type": "stream",
            "to": 10,
            "topic": old_topic,
            "content": "local reply",
        }
        before = {key: dict(value) for key, value in self.client.messages.items()}
        self.reload()
        self.client.calls.clear()
        result = self.service.run_once()
        self.assertEqual((result.renamed, result.sent, result.updated, result.failed), (1, 0, 0, 0))
        desired = "✔ " + "x" * 52 + " · f76"
        self.assertEqual(self.client.calls, ["rename"])
        self.assertEqual(set(self.client.messages), set(before))
        for key, message in self.client.messages.items():
            self.assertEqual(message, {**before[key], "topic": desired})
        self.reload()
        self.assertEqual(self.state.topic_for(task.id, "unused"), desired)
        self.client.calls.clear()
        self.assertEqual(self.service.run_once().renamed, 0)
        self.assertEqual(self.client.calls, [])

    def test_failed_rename_leaves_checkpoint_and_discussion_unchanged_then_retries(self):
        self.service.run_once()
        before = self.path.read_bytes()
        original = {key: dict(value) for key, value in self.client.messages.items()}
        self.github.tasks.return_value = ([replace(self.task, title="New title")], 0)
        self.github.discussions.return_value = [
            self.body,
            self.comment,
            replace(self.comment, id="C_2"),
        ]
        self.client.fail = "rename"
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            result = self.service.run_once()
        self.assertEqual((result.failed, result.renamed, result.sent, result.updated), (1, 0, 0, 0))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.messages, original)
        self.reload()
        self.client.fail = None
        result = self.service.run_once()
        self.assertEqual((result.failed, result.renamed, result.sent, result.updated), (0, 1, 1, 1))

    def test_lost_rename_response_is_recovered_without_a_second_rename(self):
        self.service.run_once()
        before = self.path.read_bytes()
        changed = replace(self.task, title="New title")
        self.github.tasks.return_value = ([changed], 0)
        self.client.messages[999] = {
            "type": "stream",
            "to": 10,
            "topic": topic_name(changed),
            "content": "other task",
        }
        original_update = self.client.update_message

        def update_and_lose_response(request):
            original_update(request)
            raise requests.Timeout("response lost")

        with patch.object(self.client, "update_message", side_effect=update_and_lose_response):
            with self.assertLogs("astrochat.project_sync", level="ERROR"):
                self.assertEqual(self.service.run_once().failed, 1)
        self.assertEqual(self.path.read_bytes(), before)
        desired = topic_name(changed, hash_length=4)
        self.assertEqual(self.client.messages[1]["topic"], desired)
        self.assertEqual(self.state.topic_hash_length(changed.id), 3)
        self.reload()
        result = self.service.run_once()
        self.assertEqual((result.failed, result.renamed, result.updated), (0, 1, 1))
        self.assertEqual(self.client.calls.count("rename"), 1)
        self.assertEqual(self.state.topic_for(self.task.id, "unused"), desired)
        self.assertEqual(self.state.topic_hash_length(changed.id), 4)

    def test_rename_checkpoint_survives_later_content_edit_failure(self):
        self.service.run_once()
        changed = replace(self.task, title="New title")
        self.github.tasks.return_value = ([changed], 0)
        self.client.fail = "update"
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            result = self.service.run_once()
        self.assertEqual((result.failed, result.renamed), (1, 1))
        self.reload()
        self.assertEqual(self.state.topic_for(self.task.id, "unused"), topic_name(changed))
        self.client.fail = None
        result = self.service.run_once()
        self.assertEqual((result.renamed, result.updated), (0, 1))
        self.assertEqual(self.client.calls.count("rename"), 1)

    def test_failed_topic_checkpoint_is_reconciled_after_restart(self):
        self.service.run_once()
        before = self.path.read_bytes()
        changed = replace(self.task, title="New title")
        self.github.tasks.return_value = ([changed], 0)
        self.client.messages[999] = {
            "type": "stream",
            "to": 10,
            "topic": topic_name(changed),
            "content": "other task",
        }
        with patch.object(
            self.state, "save_topic", side_effect=sqlite3.OperationalError("disk full")
        ):
            with self.assertLogs("astrochat.project_sync", level="ERROR"):
                self.assertEqual(self.service.run_once().failed, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.reload()
        result = self.service.run_once()
        self.assertEqual((result.failed, result.renamed, result.updated), (0, 1, 1))
        self.assertEqual(self.client.calls.count("rename"), 1)

        self.assertEqual(self.state.topic_hash_length(changed.id), 4)
        self.assertEqual(
            self.state.topic_for(changed.id, "unused"), topic_name(changed, hash_length=4)
        )

    def test_stop_after_rename_saves_topic_before_stopping_content_updates(self):
        self.service.run_once()
        changed = replace(self.task, title="New title")
        self.github.tasks.return_value = ([changed], 0)
        stop = Event()
        original_update = self.client.update_message

        def rename_and_stop(request):
            response = original_update(request)
            stop.set()
            return response

        with patch.object(self.client, "update_message", side_effect=rename_and_stop):
            result = self.service.run_once(stop)
        self.assertEqual((result.renamed, result.updated), (1, 0))
        self.reload()
        self.assertEqual(self.state.topic_for(self.task.id, "unused"), topic_name(changed))
        self.assertEqual(self.service.run_once().updated, 1)
        self.assertEqual(self.client.calls.count("rename"), 1)

    def test_rename_preview_changes_neither_messages_nor_topic_checkpoint(self):
        self.service.run_once()
        before = self.path.read_bytes()
        original = {key: dict(value) for key, value in self.client.messages.items()}
        self.github.tasks.return_value = ([replace(self.task, title="New title")], 0)
        self.reload(dry_run=True)
        self.client.calls.clear()
        result = self.service.run_once()
        self.assertEqual((result.renamed, result.updated), (1, 1))
        self.assertIn("renamed=1", result.summary())
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.messages, original)
        self.assertEqual(self.path.read_bytes(), before)

    def test_existing_destination_extends_hash_and_preserves_both_discussions(self):
        self.service.run_once()
        original_topic = self.client.messages[1]["topic"]
        self.client.messages[998] = {
            "type": "stream",
            "to": 10,
            "topic": original_topic,
            "content": "local reply",
        }
        changed = replace(self.task, title="New title")
        self.github.tasks.return_value = ([changed], 0)
        self.client.messages[999] = {
            "type": "stream",
            "to": 10,
            "topic": topic_name(changed),
            "content": "another topic",
        }
        self.client.calls.clear()
        result = self.service.run_once()
        self.assertEqual((result.failed, result.renamed), (0, 1))
        desired = topic_name(changed, hash_length=4)
        for identifier in (1, 2, 998):
            self.assertEqual(self.client.messages[identifier]["topic"], desired)
        self.assertEqual(self.client.messages[999]["topic"], topic_name(changed))
        self.assertEqual(self.client.messages[999]["content"], "another topic")
        self.assertEqual(self.state.topic_hash_length(changed.id), 4)

    def test_initial_collision_extends_hash_and_keeps_length_after_restart_and_title_change(self):
        first = replace(self.task, id="I_8", description=replace(self.body, id="I_8"))
        second = replace(
            self.task, id="I_55", title="RESEARCH", description=replace(self.body, id="I_55")
        )
        self.assertEqual(topic_name(first).casefold(), topic_name(second).casefold())
        self.github.tasks.return_value = ([first, second], 0)
        self.github.discussions.side_effect = lambda task: [task.description]
        result = self.service.run_once()
        self.assertEqual((result.sent, result.tasks, result.failed), (2, 2, 0))
        self.assertEqual(self.client.messages[2]["topic"], topic_name(second, hash_length=4))
        self.assertEqual(self.state.topic_hash_length(second.id), 4)
        original = dict(self.client.messages[1])
        self.reload()
        self.client.calls.clear()
        self.client.reads.clear()
        result = self.service.run_once()
        self.assertEqual((result.sent, result.renamed, result.failed), (0, 0, 0))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.reads, [])
        second = replace(second, title="Other research")
        self.github.tasks.return_value = ([first, second], 0)
        result = self.service.run_once()
        self.assertEqual((result.renamed, result.failed), (1, 0))
        self.assertEqual(self.client.messages[1], original)
        self.assertEqual(self.client.messages[2]["topic"], topic_name(second, hash_length=4))

    def test_initial_topic_collision_check_is_scoped_to_the_target_channel(self):
        self.client.messages[999] = {
            "type": "stream",
            "to": 20,
            "topic": topic_name(self.task),
            "content": "other channel",
        }
        result = self.service.run_once()
        self.assertEqual((result.sent, result.failed), (2, 0))
        self.assertEqual(self.client.messages[999]["content"], "other channel")

    def test_successive_collisions_and_resolution_keep_extended_hash_and_length_limit(self):
        task = replace(self.task, title="x" * 80)
        self.github.tasks.return_value = ([task], 0)
        for length in (3, 4):
            self.client.messages[900 + length] = {
                "type": "stream",
                "to": 10,
                "topic": topic_name(task, hash_length=length),
                "content": "other task",
            }
        self.assertEqual(self.service.run_once().failed, 0)
        self.assertEqual(self.state.topic_hash_length(task.id), 5)
        self.assertEqual(len(self.client.messages[1]["topic"]), 60)
        done = replace(task, status="Done")
        self.client.messages[905] = {
            "type": "stream",
            "to": 10,
            "topic": topic_name(done, hash_length=5),
            "content": "resolved task",
        }
        self.github.tasks.return_value = ([done], 0)
        result = self.service.run_once()
        self.assertEqual((result.failed, result.renamed), (0, 1))
        self.assertEqual(self.client.messages[1]["topic"], topic_name(done, hash_length=6))
        self.assertEqual(len(self.client.messages[1]["topic"]), 60)
        for identifier in (903, 904, 905):
            del self.client.messages[identifier]
        self.reload()
        self.github.tasks.return_value = ([task], 0)
        self.assertEqual(self.service.run_once().renamed, 1)
        self.assertEqual(self.client.messages[1]["topic"], topic_name(task, hash_length=6))
        self.assertEqual(self.state.topic_hash_length(task.id), 6)

    def test_first_send_failure_retains_selected_hash_length_after_collision_disappears(self):
        self.client.messages[999] = {
            "type": "stream",
            "to": 10,
            "topic": topic_name(self.task),
            "content": "other task",
        }
        self.client.fail = "send"
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            self.assertEqual(self.service.run_once().failed, 1)
        self.assertEqual(self.state.topic_hash_length(self.task.id), 4)
        del self.client.messages[999]
        self.reload()
        self.client.fail = None
        self.assertEqual(self.service.run_once().sent, 2)
        self.assertEqual(self.client.messages[1]["topic"], topic_name(self.task, hash_length=4))

    def test_collision_preview_reserves_names_without_writing_messages_or_state(self):
        first = replace(self.task, id="I_8", description=replace(self.body, id="I_8"))
        second = replace(self.task, id="I_55", description=replace(self.body, id="I_55"))
        self.github.tasks.return_value = ([first, second], 0)
        self.github.discussions.side_effect = lambda task: [task.description]
        before = self.path.read_bytes()
        self.reload(dry_run=True)
        with patch.object(self.state, "save_topic", wraps=self.state.save_topic) as save:
            result = self.service.run_once()
        self.assertEqual((result.failed, result.sent), (0, 2))
        self.assertEqual(
            [call.args for call in save.call_args_list],
            [(first.id, topic_name(first), 3), (second.id, topic_name(second, hash_length=4), 4)],
        )
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.messages, {})
        self.assertEqual(self.path.read_bytes(), before)

    def test_collision_search_stops_at_the_topic_length_limit(self):
        task = replace(self.task, title="x" * 80, status="Done")
        self.github.tasks.return_value = ([task], 0)
        with patch.object(
            self.client,
            "get_messages",
            return_value={"result": "success", "messages": [{"id": 999}]},
        ) as lookup:
            with self.assertLogs("astrochat.project_sync", level="ERROR"):
                self.assertEqual(self.service.run_once().failed, 1)
        self.assertEqual(lookup.call_count, 52)
        self.assertEqual(len(topic_name(task, hash_length=54)), 60)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.state.messages_for(task.id), {})

    def test_initial_topic_lookup_failure_does_not_send_messages(self):
        with patch.object(
            self.client, "get_messages", return_value={"result": "error", "msg": "lookup failed"}
        ):
            with self.assertLogs("astrochat.project_sync", level="ERROR"):
                self.assertEqual(self.service.run_once().failed, 1)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.state.messages_for(self.task.id), {})
        self.assertEqual(self.service.run_once().sent, 2)

    def test_manually_moved_or_deleted_anchor_does_not_rename_unrelated_discussions(self):
        self.service.run_once()
        before = self.path.read_bytes()
        original = dict(self.client.messages[1])
        self.github.tasks.return_value = ([replace(self.task, title="New title")], 0)
        for moved in ({**original, "to": 20}, {**original, "topic": "Other"}, None):
            with self.subTest(moved=moved):
                self.client.messages[1] = moved
                if moved is None:
                    del self.client.messages[1]
                self.client.calls.clear()
                with self.assertLogs("astrochat.project_sync", level="ERROR"):
                    self.assertEqual(self.service.run_once().failed, 1)
                self.assertEqual(self.client.calls, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_case_only_rename_is_allowed_and_truncated_title_edits_do_not_rename(self):
        self.service.run_once()
        self.github.tasks.return_value = ([replace(self.task, title="RESEARCH")], 0)
        self.assertEqual(self.service.run_once().renamed, 1)
        self.assertEqual(
            self.client.messages[1]["topic"], topic_name(replace(self.task, title="RESEARCH"))
        )
        self.github.tasks.return_value = ([replace(self.task, title="x" * 80 + "A")], 0)
        self.assertEqual(self.service.run_once().renamed, 1)
        self.github.tasks.return_value = ([replace(self.task, title="x" * 80 + "B")], 0)
        result = self.service.run_once()
        self.assertEqual((result.renamed, result.updated), (0, 1))

    def test_title_change_after_failed_first_send_needs_no_remote_rename(self):
        self.client.fail = "send"
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            self.assertEqual(self.service.run_once().failed, 1)
        self.github.tasks.return_value = ([replace(self.task, title="New title")], 0)
        self.client.fail = None
        self.reload()
        result = self.service.run_once()
        self.assertEqual((result.failed, result.renamed, result.sent), (0, 0, 2))
        self.assertNotIn("rename", self.client.calls)
        self.assertTrue(all(isinstance(request, dict) for request in self.client.reads))
        self.assertEqual(
            self.client.messages[1]["topic"], topic_name(replace(self.task, title="New title"))
        )

    def test_failed_send_resumes_from_last_success_after_restart(self):
        original_send = self.client.send_message
        with patch.object(
            self.client,
            "send_message",
            side_effect=[
                original_send({"content": render(self.task, self.body)}),
                {"result": "error", "msg": "network failure"},
            ],
        ):
            with self.assertLogs("astrochat.project_sync", level="ERROR"):
                self.assertEqual(self.service.run_once().failed, 1)
        self.reload()
        self.assertEqual(self.service.run_once().sent, 1)
        self.assertEqual(len(self.client.messages), 2)

    def test_failed_edit_and_delete_are_retried_without_advancing_state(self):
        self.service.run_once()
        self.github.discussions.return_value = [self.body, replace(self.comment, body="Edited")]
        for action in ["update", "delete"]:
            with self.subTest(action=action):
                if action == "delete":
                    self.github.discussions.return_value = [self.body]
                before = self.path.read_bytes()
                self.client.fail = action
                with self.assertLogs("astrochat.project_sync", level="ERROR"):
                    self.assertEqual(self.service.run_once().failed, 1)
                self.assertEqual(self.path.read_bytes(), before)
                self.reload()
                self.client.fail = None
                result = self.service.run_once()
                self.assertEqual(result.failed, 0)
                self.assertEqual(result.updated if action == "update" else result.deleted, 1)

    def test_partial_fetch_or_missing_task_never_deletes_history(self):
        self.service.run_once()
        self.github.discussions.side_effect = GitHubError("second page unavailable")
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            self.assertEqual(self.service.run_once().failed, 1)
        self.github.tasks.return_value = ([], 1)
        self.assertEqual(self.service.run_once().skipped, 1)
        self.assertEqual(len(self.client.messages), 2)
        self.assertNotIn("delete", self.client.calls)

    def test_task_failure_does_not_block_other_tasks(self):
        other = replace(self.task, id="I_2", description=replace(self.body, id="I_2"))
        self.github.tasks.return_value = ([self.task, other], 0)
        self.github.discussions.side_effect = [GitHubError("no access"), [other.description]]
        with self.assertLogs("astrochat.project_sync", level="ERROR"):
            result = self.service.run_once()
        self.assertEqual((result.tasks, result.failed, result.sent), (1, 1, 1))

    def test_dry_run_changes_neither_zulip_nor_state_file(self):
        self.service.run_once()
        before = self.path.read_bytes()
        self.github.discussions.return_value = [
            replace(self.body, body="new"),
            replace(self.comment, id="C_2"),
        ]
        self.reload(dry_run=True)
        self.client.calls.clear()
        result = self.service.run_once()
        self.assertEqual((result.sent, result.updated, result.deleted), (1, 1, 1))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_new_dry_run_does_not_create_state(self):
        preview_path = self.path.with_name("preview.sqlite3")
        self.service.state = self.resources.enter_context(open_state(preview_path, True))
        self.assertEqual(self.service.run_once().sent, 2)
        self.assertFalse(preview_path.exists())
        self.assertEqual(self.client.calls, [])

    def test_background_worker_uses_sqlite_and_closes_before_reopen(self):
        finished = Event()
        results = []
        original_run = self.service.run_once

        def sync(project, stop):
            try:
                result = original_run(stop)
                results.append(result)
                return result
            finally:
                finished.set()

        store = Mock()
        store.claim_due.side_effect = [Mock(id="project-1"), None]
        worker = SyncWorker(store, sync)
        worker.thread.start()
        try:
            self.assertTrue(finished.wait(5))
        finally:
            worker.close()
        self.assertEqual((results[0].sent, results[0].failed), (2, 0))
        self.reload()
        self.assertEqual(self.service.run_once().sent, 0)

    def test_long_message_is_complete_and_surplus_parts_are_deleted_on_edit(self):
        long = replace(self.comment, body="讨论" * 12000)
        self.github.discussions.return_value = [self.body, long]
        self.assertGreater(self.service.run_once().sent, 2)
        parts = [message["content"] for key, message in self.client.messages.items() if key != 1]
        self.assertTrue(all(len(part) <= 10000 for part in parts))
        restored = "".join(part.split("\n\n", 1)[1] for part in parts)
        self.assertEqual(restored, render(self.task, long))
        self.github.discussions.return_value = [self.body, self.comment]
        result = self.service.run_once()
        self.assertEqual((result.updated, result.deleted), (1, len(parts) - 1))

    def test_destination_binding_prevents_reusing_state_for_another_project(self):
        self.service.run_once()
        self.github.project.return_value = {"id": "P_2"}
        with self.assertRaisesRegex(ValueError, "different project"):
            self.service.run_once()
        self.assertEqual(len(self.client.messages), 2)

    def test_stop_mid_task_checkpoints_and_does_not_delete_unsent_remainder(self):
        stop = Event()
        original_send = self.client.send_message

        def send_and_stop(message):
            response = original_send(message)
            stop.set()
            return response

        with patch.object(self.client, "send_message", side_effect=send_and_stop):
            self.service.run_once(stop)
        self.reload()
        self.assertEqual(self.service.run_once().sent, 1)
        self.assertNotIn("delete", self.client.calls)

    def test_topic_names_are_bounded_and_distinguish_same_titles(self):
        task = replace(self.task, title="支持多个 Project 同步")
        self.assertRegex(topic_name(task, "P_1"), r"^支持多个 Project 同步 · [0-9a-f]{3}$")
        long = replace(self.task, title="title\n" * 100)
        self.assertLessEqual(len(topic_name(long)), 60)
        self.assertNotIn("\n", topic_name(long))
        self.assertNotEqual(topic_name(long), topic_name(replace(long, id="I_2")))
        self.assertNotEqual(topic_name(long, "P_1"), topic_name(long, "P_2"))

    def test_github_text_cannot_ping_zulip_users_and_preserves_email(self):
        entry = replace(self.comment, body="@**all** @*team* alice@example.com")
        content = render(self.task, entry)
        self.assertNotIn("@**all**", content)
        self.assertNotIn("@*team*", content)
        self.assertIn("alice@example.com", content)

    def test_converted_markdown_updates_existing_messages_and_removes_surplus_code_parts(self):
        body = replace(self.body, body="- [ ] pending\n\n> [!NOTE]\n> Read this")
        comment = replace(self.comment, body="```python\n" + "print(1)\n" * 3000 + "```")
        self.github.discussions.return_value = [body, comment]
        imported = self.service.run_once()
        self.assertGreater(imported.sent, 2)
        self.assertIn("☐ pending", self.client.messages[1]["content"])
        self.assertIn("> **NOTE**", self.client.messages[1]["content"])
        self.client.messages[999] = {
            "content": "local reply",
            "topic": self.client.messages[1]["topic"],
        }

        self.github.discussions.return_value = [
            replace(body, body="- [x] done\n\nInline $x^2$"),
            replace(comment, body="```python\nprint(2)\n```"),
        ]
        updated = self.service.run_once()
        self.assertEqual(
            (updated.sent, updated.updated, updated.deleted), (0, 2, imported.sent - 2)
        )
        self.assertIn("☑ done", self.client.messages[1]["content"])
        self.assertIn("$$x^2$$", self.client.messages[1]["content"])
        self.assertIn("print(2)", self.client.messages[2]["content"])
        self.assertEqual(self.client.messages[999]["content"], "local reply")

    def test_corrected_punctuation_updates_old_mirror_without_replacing_message(self):
        source = "Astrochat should be able to synchronize this message : )"
        entry = replace(self.body, body=source)
        self.github.discussions.return_value = [entry]
        old_content = render(self.task, entry).replace(source, source.replace(")", r"\)"))
        with patch("astrochat.project_sync.render", return_value=old_content):
            self.assertEqual(self.service.run_once().sent, 1)
        self.client.messages[999] = {
            "content": "local reply",
            "topic": self.client.messages[1]["topic"],
        }
        result = self.service.run_once()
        self.assertEqual((result.sent, result.updated, result.deleted), (0, 1, 0))
        self.assertIn(source, self.client.messages[1]["content"])
        self.assertNotIn(r"\)", self.client.messages[1]["content"])
        self.assertEqual(self.client.messages[999]["content"], "local reply")
        self.assertEqual(self.service.run_once().updated, 0)

    def test_images_update_existing_mirror_reuse_upload_on_restart_and_keep_local_replies(self):
        self.service.run_once()
        self.client.messages[999] = {
            "content": "local reply",
            "topic": self.client.messages[1]["topic"],
        }
        source = "https://github.com/user-attachments/assets/abc-def"
        body = replace(self.body, body=f'<img src="{source}">')
        comment = replace(self.comment, body=f"![same image]({source})")
        self.github.discussions.return_value = [body, comment]
        with patch(
            "astrochat.image_sync.ImageSync.upload", return_value="/user_uploads/1/a.png"
        ) as upload:
            result = self.service.run_once()
            self.assertEqual((result.updated, result.sent, result.failed), (2, 0, 0))
            upload.assert_called_once()
            self.reload()
            self.assertEqual(self.service.run_once().updated, 0)
            upload.assert_called_once()
        self.assertIn("/user_uploads/1/a.png", self.client.messages[1]["content"])
        self.assertNotIn(source, self.client.messages[1]["content"])
        self.assertEqual(self.client.messages[999]["content"], "local reply")

    def test_failed_image_keeps_old_topic_messages_and_deletions_pending(self):
        self.service.run_once()
        original = {key: dict(value) for key, value in self.client.messages.items()}
        source = "https://github.com/user-attachments/assets/abc-def"
        self.github.tasks.return_value = ([replace(self.task, title="New title")], 0)
        self.github.discussions.return_value = [replace(self.body, body=f"![image]({source})")]
        self.client.calls.clear()
        with patch(
            "astrochat.image_sync.ImageSync.upload", side_effect=ImageSyncError("download failed")
        ):
            with self.assertLogs("astrochat.project_sync", "ERROR"):
                result = self.service.run_once()
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.client.messages, original)
        self.assertEqual(self.client.calls, [])
        with patch("astrochat.image_sync.ImageSync.upload", return_value="/user_uploads/1/a.png"):
            result = self.service.run_once()
        self.assertEqual((result.updated, result.deleted, result.renamed), (1, 1, 1))

    def test_successful_image_checkpoint_survives_message_send_failure(self):
        source = "https://github.com/user-attachments/assets/abc-def"
        self.github.discussions.return_value = [replace(self.body, body=f"![image]({source})")]
        self.client.fail = "send"
        with patch(
            "astrochat.image_sync.ImageSync.upload", return_value="/user_uploads/1/a.png"
        ) as upload:
            with self.assertLogs("astrochat.project_sync", "ERROR"):
                self.assertEqual(self.service.run_once().failed, 1)
            self.reload()
            self.client.fail = None
            self.assertEqual(self.service.run_once().sent, 1)
            upload.assert_called_once()

    def test_preview_images_report_edits_without_download_upload_or_disk_changes(self):
        self.service.run_once()
        before = self.path.read_bytes()
        source = "https://github.com/user-attachments/assets/abc-def"
        self.github.discussions.return_value = [
            replace(self.body, body=f"![image]({source})"),
            self.comment,
        ]
        self.reload(dry_run=True)
        self.client.calls.clear()
        with patch("astrochat.image_sync.ImageSync.upload") as upload:
            self.assertEqual(self.service.run_once().updated, 1)
            upload.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_cancelling_during_image_preparation_keeps_checkpoint_without_publishing(self):
        stop = Event()
        source = "https://github.com/user-attachments/assets/abc-def"
        self.github.discussions.return_value = [replace(self.body, body=f"![image]({source})")]

        def upload(*args):
            stop.set()
            return "/user_uploads/1/a.png"

        with patch("astrochat.image_sync.ImageSync.upload", side_effect=upload):
            result = self.service.run_once(stop)
        self.assertEqual((result.failed, result.sent), (0, 0))
        self.assertEqual(self.client.messages, {})
        stop.clear()
        with patch("astrochat.image_sync.ImageSync.upload") as upload:
            self.assertEqual(self.service.run_once(stop).sent, 1)
            upload.assert_not_called()


class MessageTests(unittest.TestCase):
    def test_splitting_at_exact_limit_does_not_add_a_part(self):
        self.assertEqual(split_message("x" * 10000), ["x" * 10000])
