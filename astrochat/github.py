"""Read GitHub Projects V2 tasks and their complete, published discussions."""

from dataclasses import dataclass
from typing import Any

import requests


class GitHubError(RuntimeError):
    pass


COMMENT_FIELDS = "id body url createdAt author { login }"
TASK_FIELDS = """
    id title body url number createdAt author { login }
    repository { nameWithOwner }
"""
ASSIGNEE_FIELDS = """
    assignees(first: 100) {
        nodes { login }
        pageInfo { hasNextPage endCursor }
    }
"""
ITEM_FIELDS = """
    id
    status: fieldValueByName(name: "Status") {
        ... on ProjectV2ItemFieldSingleSelectValue { name }
    }
    content {
        __typename
        ... on Issue { %s }
        ... on PullRequest { %s }
        ... on DraftIssue { id title body createdAt creator { login } %s }
    }
""" % (TASK_FIELDS + ASSIGNEE_FIELDS, TASK_FIELDS + ASSIGNEE_FIELDS, ASSIGNEE_FIELDS)
ACTIVITY_TYPES = "PROJECT_V2_ITEM_STATUS_CHANGED_EVENT, ASSIGNED_EVENT, UNASSIGNED_EVENT"
ACTIVITY_FIELDS = """
    __typename
    ... on ProjectV2ItemStatusChangedEvent {
        id actor { login } createdAt project { id } previousStatus status wasAutomated
    }
    ... on AssignedEvent {
        id actor { login } createdAt assignee { ... on Actor { login } }
    }
    ... on UnassignedEvent {
        id actor { login } createdAt assignee { ... on Actor { login } }
    }
"""


@dataclass(frozen=True)
class Discussion:
    id: str
    body: str
    url: str
    author: str
    created_at: str
    kind: str
    is_event: bool = False


@dataclass(frozen=True)
class Task:
    id: str
    type: str
    title: str
    reference: str
    description: Discussion
    project_id: str | None = None
    status: str | None = None
    assignees: tuple[str, ...] = ()


def discussion(node: dict[str, Any], kind: str) -> Discussion:
    author = node.get("author") or node.get("creator") or {}
    return Discussion(
        id=node["id"],
        body=node["body"],
        url=node["url"],
        author=author.get("login", "deleted-user"),
        created_at=node.get("submittedAt") or node["createdAt"],
        kind=kind,
    )


def activity(node: dict[str, Any], task: Task) -> Discussion | None:
    """Turn a timeline event into literal text, scoped to this Project."""
    actor = (node.get("actor") or {}).get("login", "deleted-user")
    if node["__typename"] == "ProjectV2ItemStatusChangedEvent":
        if (node["project"] or {}).get("id") != task.project_id:
            return None
        previous = node["previousStatus"] or "(unset)"
        current = node["status"] or "(unset)"
        body = f"Project status: {previous} → {current}"
        kind = "Project status"
        if node["wasAutomated"]:
            kind += " · automation"
            actor = (node.get("actor") or {}).get("login", "github-project-automation")
    else:
        assignee = (node["assignee"] or {}).get("login", "deleted-user")
        action = "Assigned to" if node["__typename"] == "AssignedEvent" else "Unassigned from"
        body, kind = f"{action} {assignee}", "Assignment"
    return Discussion(node["id"], body, task.description.url, actor, node["createdAt"], kind, True)


class GitHubClient:
    def __init__(self, token: str, session: requests.Session) -> None:
        self.session = session
        self.session.headers.update({"Authorization": f"Bearer {token}", "User-Agent": "astrochat"})

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        # GraphQL may return HTTP 200 with both partial data and errors.
        # Partial snapshots must never drive deletion of mirrored messages.
        if payload.get("errors"):
            raise GitHubError("; ".join(error["message"] for error in payload["errors"]))
        return payload["data"]

    def project(self, owner: str, number: int, owner_type: str) -> dict[str, Any]:
        if owner_type not in {"user", "organization"}:
            raise ValueError("owner_type must be user or organization")
        query = (
            """
            query($owner: String!, $number: Int!) {
                %s(login: $owner) { projectV2(number: $number) { id title url } }
            }
        """
            % owner_type
        )
        data = self.query(query, {"owner": owner, "number": number})
        account = data[owner_type]
        if account is None or account["projectV2"] is None:
            raise GitHubError("Project not found or not accessible to the GitHub token")
        return account["projectV2"]

    def connection(
        self,
        node_id: str,
        node_type: str,
        field: str,
        fields: str,
        *,
        cursor: str | None = None,
        arguments: str = "",
    ) -> list[dict[str, Any]]:
        # Types and field selections come only from the constants/call sites below.
        query = """
            query($id: ID!, $cursor: String) {
                node(id: $id) { ... on %s {
                    %s(first: 100, after: $cursor %s) {
                        nodes { %s }
                        pageInfo { hasNextPage endCursor }
                    }
                } }
            }
        """ % (node_type, field, arguments, fields)
        nodes = []
        seen_cursors = {cursor} if cursor is not None else set()
        while True:
            data = self.query(query, {"id": node_id, "cursor": cursor})
            if data["node"] is None:
                raise GitHubError(f"GitHub object {node_id} is no longer accessible")
            page = data["node"][field]
            if any(node is None for node in page["nodes"]):
                raise GitHubError(f"Incomplete {field} snapshot for {node_id}")
            nodes.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return nodes
            cursor = page["pageInfo"]["endCursor"]
            if cursor is None or cursor in seen_cursors:
                raise GitHubError(f"Invalid pagination cursor for {node_id}/{field}")
            seen_cursors.add(cursor)

    def tasks(self, project: dict[str, Any]) -> tuple[list[Task], int]:
        items = self.connection(project["id"], "ProjectV2", "items", ITEM_FIELDS)
        tasks = []
        skipped = 0
        for item in items:
            content = item["content"]
            if content is None:
                # Redacted items and removed/inaccessible content are not deletions.
                skipped += 1
                continue
            kind = content["__typename"]
            if kind == "DraftIssue":
                content = {**content, "url": project["url"]}
                reference = "draft"
            else:
                reference = f"{content['repository']['nameWithOwner']}#{content['number']}"
            tasks.append(
                Task(
                    content["id"],
                    kind,
                    content["title"],
                    reference,
                    discussion(content, kind),
                    project["id"],
                    (item["status"] or {}).get("name"),
                    self.assignees(content),
                )
            )
        return tasks, skipped

    def assignees(self, content: dict[str, Any]) -> tuple[str, ...]:
        page = content["assignees"]
        nodes = list(page["nodes"])
        if page["pageInfo"]["hasNextPage"]:
            cursor = page["pageInfo"]["endCursor"]
            if cursor is None:
                raise GitHubError(f"Invalid assignees cursor for {content['id']}")
            nodes.extend(
                self.connection(
                    content["id"], content["__typename"], "assignees", "login", cursor=cursor
                )
            )
        return tuple(sorted(node["login"] for node in nodes))

    def discussions(self, task: Task) -> list[Discussion]:
        if task.type == "DraftIssue":
            return [task.description]
        comments = self.connection(task.id, task.type, "comments", COMMENT_FIELDS)
        entries = [discussion(comment, "Comment") for comment in comments]
        if task.type == "PullRequest":
            entries.extend(self.reviews(task.id))
        events = self.connection(
            task.id,
            task.type,
            "timelineItems",
            ACTIVITY_FIELDS,
            arguments=f", itemTypes: [{ACTIVITY_TYPES}]",
        )
        for node in events:
            entry = activity(node, task)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda entry: (entry.created_at, entry.id))
        return [task.description, *entries]

    def reviews(self, pull_request_id: str) -> list[Discussion]:
        reviews = self.connection(
            pull_request_id, "PullRequest", "reviews", COMMENT_FIELDS + " state submittedAt"
        )
        entries = [
            discussion(review, f"Review · {review['state']}")
            for review in reviews
            if review["state"] != "PENDING"
        ]
        threads = self.connection(pull_request_id, "PullRequest", "reviewThreads", "id")
        for thread in threads:
            comments = self.connection(
                thread["id"],
                "PullRequestReviewThread",
                "comments",
                COMMENT_FIELDS + " state path line originalLine",
            )
            for comment in comments:
                if comment["state"] == "PENDING":
                    continue
                line = comment["line"] or comment["originalLine"]
                location = comment["path"] + (f":{line}" if line else "")
                entries.append(discussion(comment, f"Code review · {location}"))
        return entries
