import unittest
from unittest.mock import Mock

import requests

from astrochat.github import GitHubClient, GitHubError


def comment(identifier, **changes):
    return {
        "id": identifier,
        "body": f"body {identifier}",
        "url": f"https://github.com/example/repo/issues/1#{identifier}",
        "createdAt": "2026-10-18T00:00:00Z",
        "author": {"login": "alice"},
        **changes,
    }


def item(identifier="I_1", kind="Issue", **changes):
    return {
        "id": f"PVTI_{identifier}",
        "status": {"name": "Backlog"},
        "content": {
            **comment(identifier),
            "__typename": kind,
            "title": "Research",
            "number": 1,
            "repository": {"nameWithOwner": "example/repo"},
            "assignees": {
                "nodes": [{"login": "alice"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
            **changes,
        },
    }


def page(field, nodes, cursor=None):
    return {
        "data": {
            "node": {
                field: {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                }
            }
        }
    }


def event(identifier, kind="ProjectV2ItemStatusChangedEvent", **changes):
    return {
        "id": identifier,
        "__typename": kind,
        "createdAt": "2026-10-18T00:01:00Z",
        "actor": {"login": "alice"},
        "project": {"id": "P_1"},
        "previousStatus": "Backlog",
        "status": "In progress",
        "wasAutomated": False,
        "assignee": {"login": "bob"},
        **changes,
    }


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        self.github = GitHubClient("test-token", self.session)
        self.project = {"id": "P_1", "url": "https://github.com/users/example/projects/1"}

    def responses(self, *payloads):
        self.session.post.side_effect = [
            Mock(json=Mock(return_value=payload)) for payload in payloads
        ]

    def test_project_query_uses_variables_and_supports_personal_projects(self):
        self.responses({"data": {"user": {"projectV2": self.project}}})
        self.assertEqual(self.github.project("example", 1, "user"), self.project)
        request = self.session.post.call_args.kwargs
        self.assertEqual(request["json"]["variables"], {"owner": "example", "number": 1})
        self.assertEqual(request["timeout"], 30)
        self.assertEqual(self.session.headers["Authorization"], "Bearer test-token")

    def test_tasks_and_comments_follow_all_pages(self):
        self.responses(
            page("items", [item()], "items-2"),
            page("items", [item("I_2"), {"id": "hidden", "content": None}]),
            page("comments", [comment("C_1")], "comments-2"),
            page("comments", [comment("C_2", author=None)]),
            page("timelineItems", []),
        )
        tasks, skipped = self.github.tasks(self.project)
        entries = self.github.discussions(tasks[0])
        self.assertEqual([task.id for task in tasks], ["I_1", "I_2"])
        self.assertEqual(skipped, 1)
        self.assertEqual([entry.id for entry in entries], ["I_1", "C_1", "C_2"])
        self.assertEqual(entries[-1].author, "deleted-user")
        cursors = [
            call.kwargs["json"]["variables"]["cursor"] for call in self.session.post.call_args_list
        ]
        self.assertEqual(cursors, [None, "items-2", None, "comments-2", None])
        self.assertEqual((tasks[0].status, tasks[0].assignees), ("Backlog", ("alice",)))

    def test_partial_graphql_errors_fail_instead_of_using_partial_snapshot(self):
        self.responses({"data": {"node": None}, "errors": [{"message": "rate limited"}]})
        with self.assertRaisesRegex(GitHubError, "rate limited"):
            self.github.tasks(self.project)

    def test_later_page_failure_never_returns_partial_comments(self):
        self.responses(page("comments", [comment("C_1")], "next"), {"data": {"node": None}})
        with self.assertRaises(GitHubError):
            self.github.connection("I_1", "Issue", "comments", "id")

    def test_http_errors_propagate(self):
        self.session.post.return_value.raise_for_status.side_effect = requests.HTTPError("403")
        with self.assertRaises(requests.HTTPError):
            self.github.project("example", 1, "user")

    def test_invalid_pagination_and_null_nodes_fail(self):
        for payloads in [
            [page("comments", [None])],
            [page("comments", [], "same"), page("comments", [], "same")],
        ]:
            with self.subTest(payloads=payloads):
                self.responses(*payloads)
                with self.assertRaises(GitHubError):
                    self.github.connection("I_1", "Issue", "comments", "id")

    def test_draft_uses_creator_and_has_no_comment_query(self):
        draft = item("D_1", "DraftIssue", creator={"login": "bob"})
        del draft["content"]["author"]
        del draft["content"]["url"]
        self.responses(page("items", [draft]))
        tasks, _ = self.github.tasks(self.project)
        entries = self.github.discussions(tasks[0])
        self.assertEqual(entries[0].author, "bob")
        self.assertEqual(entries[0].url, self.project["url"])
        self.assertEqual(self.session.post.call_count, 1)

    def test_pr_includes_reviews_and_all_thread_pages_but_not_pending_drafts(self):
        self.responses(
            page("items", [item("PR_1", "PullRequest")]),
            page("comments", [comment("C_1", createdAt="2026-10-18T00:03:00Z")]),
            page("reviews", [comment("R_1", state="APPROVED", submittedAt=None)], "r2"),
            page("reviews", [comment("R_pending", state="PENDING", submittedAt=None)]),
            page("reviewThreads", [{"id": "T_1"}], "t2"),
            page("reviewThreads", [{"id": "T_2"}]),
            page(
                "comments",
                [comment("RC_1", state="SUBMITTED", path="a.py", line=7, originalLine=7)],
                "c2",
            ),
            page(
                "comments",
                [comment("RC_2", state="SUBMITTED", path="a.py", line=None, originalLine=8)],
            ),
            page(
                "comments",
                [comment("RC_pending", state="PENDING", path="b.py", line=1, originalLine=1)],
            ),
            page("timelineItems", [event("E_1", "AssignedEvent")]),
        )
        tasks, _ = self.github.tasks(self.project)
        entries = self.github.discussions(tasks[0])
        self.assertEqual(
            {entry.id for entry in entries}, {"PR_1", "C_1", "R_1", "RC_1", "RC_2", "E_1"}
        )
        self.assertEqual(entries[-1].id, "C_1")
        self.assertIn("a.py:8", next(entry.kind for entry in entries if entry.id == "RC_2"))

    def test_status_history_includes_intermediate_states_automation_and_only_this_project(self):
        self.responses(
            page("items", [item()]),
            page("comments", [comment("C_1", createdAt="2026-10-18T00:02:00Z")]),
            page(
                "timelineItems",
                [
                    event(
                        "E_1",
                        previousStatus="",
                        status="Backlog",
                        actor={"login": "github-project-automation"},
                    ),
                    event("E_2"),
                ],
                "events-2",
            ),
            page(
                "timelineItems",
                [
                    event(
                        "E_3",
                        previousStatus="In progress",
                        status="Done",
                        createdAt="2026-10-18T00:03:00Z",
                        wasAutomated=True,
                        actor=None,
                    ),
                    event("E_other", project={"id": "P_other"}),
                    event("E_hidden", project=None),
                ],
            ),
        )
        tasks, _ = self.github.tasks(self.project)
        entries = self.github.discussions(tasks[0])
        self.assertEqual([entry.id for entry in entries], ["I_1", "E_1", "E_2", "C_1", "E_3"])
        self.assertEqual(
            [entry.body for entry in entries if entry.is_event],
            [
                "Project status: (unset) → Backlog",
                "Project status: Backlog → In progress",
                "Project status: In progress → Done",
            ],
        )
        self.assertEqual(entries[1].author, "github-project-automation")
        self.assertEqual(entries[-1].author, "github-project-automation")
        self.assertIn("automation", entries[-1].kind)
        request = self.session.post.call_args.kwargs["json"]
        self.assertEqual(request["variables"]["cursor"], "events-2")
        self.assertIn("itemTypes: [PROJECT_V2_ITEM_STATUS_CHANGED_EVENT", request["query"])

    def test_assignment_and_unassignment_record_actor_assignee_and_original_time(self):
        self.responses(
            page("items", [item()]),
            page("comments", []),
            page(
                "timelineItems",
                [
                    event("A_1", "AssignedEvent"),
                    event("A_2", "UnassignedEvent", actor=None),
                    event("A_3", "AssignedEvent", assignee=None),
                ],
            ),
        )
        tasks, _ = self.github.tasks(self.project)
        entries = self.github.discussions(tasks[0])[1:]
        self.assertEqual(
            [entry.body for entry in entries],
            [
                "Assigned to bob",
                "Unassigned from bob",
                "Assigned to deleted-user",
            ],
        )
        self.assertEqual(entries[0].author, "alice")
        self.assertEqual(entries[1].author, "deleted-user")
        self.assertEqual(entries[0].created_at, "2026-10-18T00:01:00Z")
        self.assertTrue(all(entry.is_event for entry in entries))

    def test_assignee_pages_are_not_silently_truncated_and_unset_status_is_supported(self):
        source = item(
            assignees={
                "nodes": [{"login": "bob"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "a2"},
            }
        )
        source["status"] = None
        self.responses(
            page("items", [source]),
            page("assignees", [{"login": "alice"}], "a3"),
            page("assignees", [{"login": "carol"}]),
        )
        tasks, _ = self.github.tasks(self.project)
        self.assertEqual(tasks[0].assignees, ("alice", "bob", "carol"))
        self.assertIsNone(tasks[0].status)
        cursors = [
            call.kwargs["json"]["variables"]["cursor"] for call in self.session.post.call_args_list
        ]
        self.assertEqual(cursors, [None, "a2", "a3"])

    def test_later_event_page_failure_does_not_return_partial_discussions(self):
        self.responses(
            page("items", [item()]),
            page("comments", [comment("C_1")]),
            page("timelineItems", [event("E_1")], "next"),
            {"data": {"node": None}},
        )
        tasks, _ = self.github.tasks(self.project)
        with self.assertRaises(GitHubError):
            self.github.discussions(tasks[0])
