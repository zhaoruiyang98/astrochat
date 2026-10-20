"""Shared GitHub account mappings managed through one-to-one Zulip messages."""

import re
from datetime import datetime, timezone
from typing import Any

import requests
import zulip

from astrochat.markdown import escape_text
from astrochat.project_store import GitHubAccount, ProjectError, ProjectStore
from astrochat.project_sync import ZulipError, checked

USAGE = """Send github commands in a one-to-one DM with AstroChat.
`github link USERNAME [@USER]`: set your own or another Zulip user's GitHub username
`github status [@USER]`: show your own or another user's mapping
`github list`: show all linked accounts
`github unlink [@USER]`: remove your own or another user's mapping
Select @USER from Zulip's mention picker; omit it to select yourself.
Everyone can edit these shared mappings. No GitHub ownership verification is performed.
Mappings do not grant Project management permissions. Never send tokens in chat."""


def target_user(sender_id: int, mention: str, members: list[dict[str, Any]]) -> int:
    if not mention:
        return sender_id
    match = re.fullmatch(r"@_?\*\*([^*\n]+)\*\*", mention)
    if match is None:
        raise ProjectError("Select one Zulip user from the @mention picker, not a plain name.")
    name = match[1]
    if "|" in name:
        name, _, identifier = name.rpartition("|")
        matches = [member for member in members if str(member["user_id"]) == identifier]
        if matches and name and name != matches[0]["full_name"]:
            raise ProjectError(
                "The mention's name and user ID do not match. Select the user again."
            )
    else:
        matches = [member for member in members if member["full_name"] == name]
    if not matches:
        raise ProjectError("Zulip user not found. Select the user again from the @mention picker.")
    if len(matches) > 1:
        raise ProjectError(
            "Several Zulip users have that name. Select a mention containing the user's ID."
        )
    return matches[0]["user_id"]


def user_label(user_id: int, names: dict[int, str]) -> str:
    name = escape_text(" ".join(names.get(user_id, "Unknown user").split()))
    return f"{name} (Zulip user ID {user_id})"


def describe_account(account: GitHubAccount, names: dict[int, str]) -> str:
    login = f"`{account.github_login}`" if account.github_login else "(not linked)"
    updated = datetime.fromtimestamp(account.updated_at, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    return (
        f"{user_label(account.user_id, names)} → {login}\n"
        f"Last changed by {user_label(account.updated_by, names)} at {updated}"
    )


class GitHubCommands:
    def __init__(self, store: ProjectStore, client: zulip.Client) -> None:
        self.store = store
        self.client = client

    def execute(self, sender_id: int, content: str) -> str:
        # Split only fixed arguments; mentions can contain spaces, quotes and apostrophes.
        parts = content.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "help"
        arguments = parts[2].strip() if len(parts) > 2 else ""
        if action not in {"link", "unlink", "status", "list"}:
            return USAGE
        if action == "list" and arguments or action == "link" and not arguments:
            return USAGE
        login = None
        if action == "link":
            values = arguments.split(maxsplit=1)
            login = values[0]
            arguments = values[1] if len(values) > 1 else ""
            if len(login) > 39 or re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", login) is None:
                return (
                    "Use a GitHub username of 1–39 letters, digits or single hyphens, "
                    "without a leading/trailing hyphen, @, URL or token."
                )
        try:
            members = checked(self.client.get_members())["members"]
            names = {member["user_id"]: member["full_name"] for member in members}
            if action == "list":
                accounts = self.store.github_accounts()
                return "\n\n".join(describe_account(account, names) for account in accounts) or (
                    "No GitHub accounts are linked. Use `github link USERNAME`."
                )
            user_id = target_user(sender_id, arguments, members)
            return self.account_action(sender_id, user_id, action, login, names)
        except ProjectError as error:
            return str(error)
        except (ZulipError, requests.RequestException):
            return "Could not look up Zulip users. No mappings were changed; try again."

    def account_action(
        self, sender_id: int, user_id: int, action: str, login: str | None, names: dict[int, str]
    ) -> str:
        if action == "status":
            account = self.store.github_account(user_id)
            if account is None:
                return f"{user_label(user_id, names)} has no GitHub account linked."
            return describe_account(account, names)
        previous, current = self.store.set_github_account(user_id, login, sender_id)
        old = f"`{previous.github_login}`" if previous and previous.github_login else "(not linked)"
        new = f"`{current.github_login}`" if current.github_login else "(not linked)"
        return (
            f"GitHub mapping updated: {old} → {new}\n"
            + describe_account(current, names)
            + "\nSynced author and assignee labels will update on the next project sync."
        )
