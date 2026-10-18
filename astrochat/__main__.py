"""Run private project management, authorization pages and background syncing."""

import argparse
import logging
import os
import signal
from contextlib import ExitStack
from pathlib import Path
from types import FrameType
from typing import Any

import zulip
from zulip_bots.lib import (
    ExternalBotHandler,
    extract_query_without_mention,
    is_private_message_but_not_group_pm,
)

from astrochat.astrochat import AstroChatBot
from astrochat.authorization import Authorization, AuthorizationServer, create_app
from astrochat.project_commands import ProjectCommands, SyncRunner, auth_origin
from astrochat.project_store import open_projects
from astrochat.project_sync import checked
from astrochat.sync_worker import SyncWorker

logger = logging.getLogger(__name__)


class ChatEvents:
    def __init__(self, bot: AstroChatBot, handler: ExternalBotHandler) -> None:
        self.bot = bot
        self.handler = handler

    def __call__(self, event: dict[str, Any]) -> None:
        if event["type"] != "message":
            return
        message = dict(event["message"])
        if message["sender_id"] == self.handler.user_id:
            return
        mentioned = "mentioned" in event["flags"]
        if not mentioned and not is_private_message_but_not_group_pm(message, self.handler):
            return
        message["full_content"] = message["content"]
        if mentioned:
            message["content"] = extract_query_without_mention(message, self.handler)
            if message["content"] is None:
                return
        self.bot.handle_message(message, self.handler)


def stop_process(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def run(args: argparse.Namespace) -> int:
    public_url = auth_origin(args.auth_url)
    with ExitStack() as resources:
        chat_client = zulip.Client(config_file=args.zulip_config)
        resources.callback(chat_client.session.close)
        bot_id = checked(chat_client.get_profile())["user_id"]
        directory = args.data_dir.expanduser().resolve()
        store = resources.enter_context(
            open_projects(directory, chat_client.base_url, chat_client.email)
        )
        # Each thread owns its API session; only the locked project registry is shared.
        sync_client = zulip.Client(config_file=args.zulip_config, retry_on_errors=False)
        resources.callback(sync_client.session.close)
        worker = SyncWorker(store, SyncRunner(store, directory, sync_client, bot_id))
        if args.once or args.dry_run:
            return worker.run_saved_once(dry_run=args.dry_run)
        command_client = zulip.Client(config_file=args.zulip_config, retry_on_errors=False)
        resources.callback(command_client.session.close)
        handler = ExternalBotHandler(chat_client, None, {"name": "AstroChat"})
        bot = AstroChatBot(ProjectCommands(store, worker, command_client, bot_id, public_url))
        worker.thread.start()
        resources.callback(worker.close)
        server = AuthorizationServer(
            create_app(Authorization(store, worker, public_url)), args.auth_port
        )
        # Close the web server first, then join syncing, then release clients and SQLite.
        resources.callback(server.close)
        server.start()
        logger.info("AstroChat is listening; authorization page: %s/authorize", public_url)
        chat_client.call_on_each_event(ChatEvents(bot, handler), ["message"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AstroChat Zulip bot and GitHub Projects sync")
    parser.add_argument(
        "--zulip-config", help="Path to zuliprc (or use ZULIP_* environment variables)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(".astrochat"),
        help="Project registry, token encryption key and sync state directory",
    )
    parser.add_argument(
        "--auth-url",
        default=os.environ.get("ASTROCHAT_AUTH_URL", "http://localhost:8765"),
        help="Public HTTPS origin for authorization links (localhost for development)",
    )
    parser.add_argument(
        "--auth-port",
        type=int,
        default=8765,
        help="Authorization server port, bound to 127.0.0.1 behind a reverse proxy",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true", help="Sync all enabled saved projects once, then exit"
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview all enabled projects without message/state writes",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    signal.signal(signal.SIGTERM, stop_process)
    try:
        return run(args)
    except KeyboardInterrupt:
        logger.info("AstroChat stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
