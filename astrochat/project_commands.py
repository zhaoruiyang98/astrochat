"""Private chat commands; identity always comes from the Zulip message sender."""

import re
import secrets
import shlex
import time
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit

import requests
import zulip

from astrochat.channel_creation import ChannelConfirmation, create_public_channel, find_channel
from astrochat.config import SyncConfig
from astrochat.github import GitHubClient
from astrochat.project_store import Project, ProjectError, ProjectStore
from astrochat.project_sync import ProjectSync, SyncResult, ZulipError, checked
from astrochat.sync_state import open_state

USAGE = """Send project commands in a one-to-one DM with AstroChat.
`project setup NAME PROJECT_URL "CHANNEL" [SECONDS]`: add a project (default: 900 seconds)
`project auth NAME`: get a new authorization link, also used to replace a token
`project list [--all]`: list your projects; --all includes everyone's projects
`project status [NAME] [--all]`: view your sync status; --all includes everyone's projects
`project sync NAME`: request an immediate sync
`project pause NAME` / `project resume NAME`: stop / resume polling
`project channel NAME "CHANNEL"`: change the destination
`project confirm CODE` / `project cancel CODE`: create a missing public channel / cancel
`project interval NAME SECONDS`: change the polling interval (minimum: 60)
`project remove NAME`: remove configuration; keep the channel and mirrored Zulip messages
Only the creator can manage or sync a project; these commands use your own NAME.
With --all, status shows all matching projects with their creators.
Missing channels require confirmation; the new public channel includes you and AstroChat.
GitHub tokens belong only on the authorization page. Never send them in chat."""


def parse_project_url(url: str) -> tuple[str, str, int, str]:
    match = re.fullmatch(
        r"https://github\.com/(users|orgs)/([A-Za-z0-9][A-Za-z0-9-]*)/projects/([1-9][0-9]{0,9})/?",
        url,
    )
    if match is None:
        raise ProjectError(
            "Use a GitHub Project URL: https://github.com/users/OWNER/projects/1 (or orgs)."
        )
    kind, owner, number = match.groups()
    if int(number) > 2147483647:
        raise ProjectError("GitHub Project number is outside the supported range.")
    return owner, "user" if kind == "users" else "organization", int(number), url.rstrip("/")


def auth_origin(url: str) -> str:
    parsed = urlsplit(url)
    local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname
        or (parsed.scheme != "https" and not local)
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Authorization URL must be an HTTPS origin (HTTP is allowed on localhost)")
    hostname = parsed.hostname.encode("idna").decode()
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        host += f":{port}"
    return f"{parsed.scheme}://{host}"


def interval_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError:
        raise ProjectError("Interval must be an integer between 60 and 86400 seconds.") from None
    if not 60 <= seconds <= 86400:
        raise ProjectError("Interval must be between 60 and 86400 seconds.")
    return seconds


def check_channel(client: zulip.Client, channel_id: int, user_id: int, bot_id: int) -> None:
    members = checked(client.call_endpoint(f"streams/{channel_id}/members", method="GET"))[
        "subscribers"
    ]
    if user_id not in members or bot_id not in members:
        raise ProjectError("You and AstroChat must both be active members of the target channel.")


def escape_label(value: str) -> str:
    """Render user-controlled names without Markdown formatting or mentions."""
    value = " ".join(value.split())
    for marker in "\\`*_[]":
        value = value.replace(marker, "\\" + marker)
    return value.replace("@", "@\u200b")


class ProjectCommands:
    def __init__(
        self, store: ProjectStore, worker, client: zulip.Client, bot_id: int, public_url: str
    ) -> None:
        self.store = store
        self.worker = worker
        self.client = client
        self.bot_id = bot_id
        self.public_url = auth_origin(public_url)
        # Only the serial chat event loop owns these short-lived prompts. They
        # are intentionally discarded on restart; saved projects are unchanged.
        self.confirmations: dict[int, ChannelConfirmation] = {}

    def execute(self, user_id: int, content: str) -> str:
        try:
            parts = shlex.split(content)
        except ValueError:
            return 'Could not parse the command. Put channel names containing spaces in "quotes".'
        try:
            return self.dispatch(user_id, parts[1:])
        except ProjectError as error:
            return str(error)
        except (ZulipError, requests.RequestException):
            return "Could not check the target channel. Check membership and try again."

    def dispatch(self, user_id: int, args: list[str]) -> str:
        if not args or args == ["help"]:
            return USAGE
        action = args[0].lower()
        if action in {"confirm", "cancel"}:
            return self.confirm_channel(user_id, action, args[1]) if len(args) == 2 else USAGE
        if action in {"list", "status"}:
            options = args[1:]
            all_users = "--all" in options
            if all_users:
                options.remove("--all")
            if len(options) > (1 if action == "status" else 0) or any(
                option.startswith("-") for option in options
            ):
                return USAGE
            name = options[0].lower() if options else None
            return self.show_projects(user_id, name, all_users=all_users)
        if action == "setup" and len(args) in {4, 5}:
            return self.setup(user_id, args)
        if len(args) < 2:
            return USAGE
        name = args[1].lower()
        # Public reads above never grant permission to mutate. Resolve management
        # commands against the sender's own names before any network request or write.
        project = self.store.get_owned(user_id, name)
        if len(args) == 2:
            return self.project_action(project, action)
        if action == "channel" and len(args) == 3:
            return self.change_channel(project, args[2])
        elif action == "interval" and len(args) == 3:
            self.store.change_owned(user_id, name, interval_seconds=interval_seconds(args[2]))
        else:
            return USAGE
        self.worker.changed(project.id)
        return "Settings updated. Existing authorization links have been invalidated."

    def setup(self, user_id: int, args: list[str], channel_id: int | None = None) -> str:
        name = args[1].lower()
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name) is None:
            raise ProjectError(
                "NAME must start with a letter and contain 1–40 letters, digits, _ or -."
            )
        owner, kind, number, url = parse_project_url(args[2])
        self.check_new_name(user_id, name)
        channel = args[3].strip()
        if not channel:
            raise ProjectError("Channel name must not be empty.")
        interval = interval_seconds(args[4]) if len(args) == 5 else 900
        if channel_id is None:
            channel_id = find_channel(self.client, channel)
        if channel_id is None:
            return self.ask_to_create(user_id, args, channel)
        check_channel(self.client, channel_id, user_id, self.bot_id)
        project = self.store.create(
            user_id, name, owner, kind, number, url, channel, channel_id, interval
        )
        return f"Created `{name}`. " + self.authorization_link(project)

    def check_new_name(self, user_id: int, name: str) -> None:
        # Check before creating a channel, so duplicate configuration names do
        # not leave behind unused channels. The database also enforces uniqueness.
        if any(project.name == name for project in self.store.list_owned(user_id)):
            raise ProjectError("You already have a project with that name. Choose another NAME.")

    def change_channel(self, project: Project, channel: str, channel_id: int | None = None) -> str:
        channel = channel.strip()
        if not channel:
            raise ProjectError("Channel name must not be empty.")
        if channel_id is None:
            channel_id = find_channel(self.client, channel)
        if channel_id is None:
            return self.ask_to_create(
                project.user_id, ["channel", project.name, channel], channel, project
            )
        check_channel(self.client, channel_id, project.user_id, self.bot_id)
        with self.store.lock:
            self.check_unchanged(project)
            self.store.change_owned(
                project.user_id, project.name, channel=channel, channel_id=channel_id
            )
        self.worker.changed(project.id)
        return "Settings updated. Existing authorization links have been invalidated."

    def check_unchanged(self, project: Project) -> None:
        current = self.store.get_owned(project.user_id, project.name)
        if (current.id, current.revision) != (project.id, project.revision):
            raise ProjectError("Project settings changed. Repeat the original command.")

    def ask_to_create(
        self, user_id: int, args: list[str], channel: str, project: Project | None = None
    ) -> str:
        if self.client.feature_level < 417:
            raise ProjectError(
                "Channel not found. Creating channels through AstroChat requires Zulip 11.0 "
                "(feature level 417) or newer. Create the channel manually, add yourself and "
                "AstroChat, then repeat the command."
            )
        now = time.monotonic()
        self.confirmations = {
            owner: pending for owner, pending in self.confirmations.items() if pending.expires > now
        }
        code = secrets.token_hex(4)
        self.confirmations[user_id] = ChannelConfirmation(
            code, tuple(args), channel, now + 600, project
        )
        return (
            f"Channel **{escape_label(channel)}** was not found. Create it as a **public "
            f"channel** for `{args[1].lower()}`?\n"
            "Synced content will be visible to organization members with access to public "
            "channels. Anonymous web access will be disabled. You and AstroChat will be "
            "added as members and channel administrators. Zulip will announce the new "
            "channel if your organization has new channel announcements enabled.\n"
            f"Confirm: `project confirm {code}`\nCancel: `project cancel {code}`\n"
            "This confirmation expires in 10 minutes and replaces your earlier pending "
            "confirmation. Settings stay unchanged until you confirm."
        )

    def confirm_channel(self, user_id: int, action: str, code: str) -> str:
        pending = self.confirmations.get(user_id)
        if pending is None or pending.code != code:
            raise ProjectError("No matching confirmation. Repeat the original command.")
        del self.confirmations[user_id]
        if pending.expires <= time.monotonic():
            raise ProjectError("Confirmation expired. Repeat the original command.")
        if action == "cancel":
            return "Channel creation cancelled. Settings were not changed."
        if pending.project is None:
            self.check_new_name(user_id, pending.args[1].lower())
        else:
            self.check_unchanged(pending.project)
        if find_channel(self.client, pending.channel) is not None:
            return (
                "That channel now exists. Settings were not saved. "
                "Check access and membership, then repeat the original command."
            )
        channel_id = create_public_channel(self.client, pending.channel, user_id, self.bot_id)
        created = f"Created public channel **{escape_label(pending.channel)}**. "
        try:
            if pending.project is None:
                reply = self.setup(user_id, list(pending.args), channel_id)
            else:
                reply = self.change_channel(pending.project, pending.channel, channel_id)
        except ProjectError as error:
            return created + "Could not save project settings: " + str(error)
        except (ZulipError, requests.RequestException):
            return created + (
                "Could not check membership to save project settings. "
                "Check the channel in Zulip, then repeat the original command."
            )
        return created + reply

    def authorization_link(self, project: Project) -> str:
        ticket = self.store.issue_ticket(project.user_id, project.name)
        self.worker.changed(project.id)
        return (
            f"[Authorize GitHub access]({self.public_url}/authorize#{ticket})\n"
            "This private link expires in 10 minutes and accepts one submission. Do not forward it. "
            "Enter the GitHub token on that page, not in Zulip."
        )

    def project_action(self, project: Project, action: str) -> str:
        user_id, name = project.user_id, project.name
        if action == "auth":
            return self.authorization_link(project)
        if action == "sync":
            self.store.request_owned(user_id, name)
            self.worker.wake.set()
            return f"Sync requested for `{name}`. Use `project status {name}`."
        if action in {"pause", "resume"}:
            self.store.change_owned(user_id, name, enabled=action == "resume")
            reply = "Paused." if action == "pause" else "Resumed."
        elif action == "remove":
            self.store.remove_owned(user_id, name)
            reply = "Configuration and saved token removed. Existing Zulip channels and messages are kept."
        else:
            return USAGE
        self.worker.changed(project.id)
        return reply + " Any in-flight request will finish before the current sync stops."

    def show_projects(
        self, user_id: int, name: str | None = None, *, all_users: bool = False
    ) -> str:
        if all_users:
            projects = self.store.list_projects(name)
        elif name is not None:
            projects = [self.store.get_owned(user_id, name)]
        else:
            projects = self.store.list_owned(user_id)
        if not projects:
            if not all_users:
                return "You have no projects. Use `project setup` or view `project list --all`."
            return (
                "No projects match that name. Use `project list --all`."
                if name is not None
                else "No projects have been configured yet. Use `project setup`."
            )
        # Resolve names once per command, including when an owner has many projects.
        try:
            members = checked(self.client.get_members())["members"]
            names = {member["user_id"]: member["full_name"] for member in members}
            notice = ""
        except (ZulipError, requests.RequestException):
            names = {}
            notice = "Creator names are temporarily unavailable; Zulip user IDs are shown.\n\n"
        return notice + "\n\n".join(
            self.describe(project, names.get(project.user_id, "Unknown user"))
            for project in projects
        )

    @staticmethod
    def describe(project: Project, creator: str) -> str:
        mode = "enabled" if project.enabled else "paused"
        auth = "authorized" if project.authorized else "needs authorization"
        return (
            f"**{project.name}** — {mode}, {auth}\n"
            f"Creator: {escape_label(creator)} (Zulip user ID {project.user_id})\n{project.url}\n"
            f"Channel: {escape_label(project.channel)} (ID {project.channel_id}); "
            f"every {project.interval_seconds}s\n{project.status}"
        )


class SyncRunner:
    """Runs one saved project; the scheduler owns its API client and sessions."""

    def __init__(self, store: ProjectStore, directory: Path, client: zulip.Client, bot_id: int):
        self.store = store
        self.directory = directory
        self.client = client
        self.bot_id = bot_id

    def __call__(self, project: Project, stop: Event, dry_run: bool = False) -> SyncResult:
        check_channel(self.client, project.channel_id, project.user_id, self.bot_id)
        config = SyncConfig(
            project.owner,
            project.number,
            project.channel,
            self.directory / "states" / f"{project.id}-{project.channel_id}.sqlite3",
            owner_type=project.owner_type,
            interval_seconds=project.interval_seconds,
            project_id=project.github_id,
            channel_id=project.channel_id,
        )
        with requests.Session() as session, open_state(config.state_file, dry_run) as state:
            github = GitHubClient(self.store.token_for(project), session)
            if stop.is_set():
                return SyncResult()
            github_users = {
                account.github_login: account.user_id for account in self.store.github_accounts()
            }
            return ProjectSync(config, github, self.client, state, github_users).run_once(stop)
