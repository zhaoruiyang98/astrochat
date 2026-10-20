from __future__ import annotations

from typing import TYPE_CHECKING, Any

from zulip_bots.lib import AbstractBotHandler

if TYPE_CHECKING:
    from astrochat.github_commands import GitHubCommands
    from astrochat.project_commands import ProjectCommands


class AstroChatBot:
    def __init__(
        self, projects: ProjectCommands | None = None, github: GitHubCommands | None = None
    ) -> None:
        self.projects = projects
        self.github = github

    def usage(self) -> str:
        return (
            "I'm AstroChat. Mention me or send a direct message.\n"
            "`help`: show commands\n"
            "`project help`: manage your GitHub Projects in a one-to-one DM\n"
            "`project list [--all]`: list your projects; --all includes everyone's projects\n"
            "`project status [NAME] [--all]`: show your sync results; --all includes everyone\n"
            "`project sync NAME`: request a sync for your own project\n"
            "`project sync --all`: sync all your authorized, unpaused projects\n"
            "Only the creator can configure, authorize, sync, pause or remove a project.\n"
            "`github help`: link GitHub usernames to Zulip users in a one-to-one DM\n"
            "`github link USERNAME [@USER]` / `github unlink [@USER]`: edit shared mappings\n"
            "`github status [@USER]` / `github list`: view account mappings\n"
            "Everyone can edit mappings; synced user labels use silent mentions.\n"
            "Submit GitHub tokens only on the authorization page, never in chat."
        )

    def handle_message(self, message: dict[str, Any], bot_handler: AbstractBotHandler) -> None:
        if message.get("sender_id") == bot_handler.user_id:
            return
        command = message["content"].strip().lower()
        if command == "help":
            bot_handler.send_reply(message, self.usage())
        elif command.split(maxsplit=1)[:1] in (["project"], ["github"]):
            namespace = command.split(maxsplit=1)[0]
            commands = self.projects if namespace == "project" else self.github
            recipients = message.get("display_recipient", [])
            private = message.get("type") == "private" and {
                recipient["id"] for recipient in recipients
            } == {bot_handler.user_id, message["sender_id"]}
            if not private:
                reply = (
                    f"Send {namespace} commands in a one-to-one DM with AstroChat. "
                    "Never post tokens in chat."
                )
            elif commands is None:
                reply = (
                    f"{namespace.capitalize()} management is not configured. "
                    "Start the bot with `python -m astrochat`."
                )
            else:
                reply = commands.execute(message["sender_id"], message["content"].strip())
            bot_handler.send_reply(message, reply)
        else:
            bot_handler.send_reply(
                message,
                "Hello! I'm AstroChat, your friendly chatbot. Send `help` to see my commands.",
            )
            bot_handler.react(message, "wave")


handler_class = AstroChatBot
