"""Explicit creation of public Zulip channels after a private-chat confirmation."""

from dataclasses import dataclass

import requests
import zulip

from astrochat.project_store import Project, ProjectError
from astrochat.project_sync import checked


@dataclass(frozen=True)
class ChannelConfirmation:
    code: str
    args: tuple[str, ...]
    channel: str
    expires: float
    project: Project | None = None


def find_channel(client: zulip.Client, channel: str) -> int | None:
    response = client.get_stream_id(channel)
    # Zulip uses BAD_REQUEST for missing names as well as other failures. Only
    # this specific lookup error may start a creation prompt; permissions and
    # transport failures must not be mistaken for an absent channel.
    if response.get("code") == "BAD_REQUEST" and response.get("msg") in {
        f"Invalid channel name '{channel}'",
        f"Invalid stream name '{channel}'",
    }:
        return None
    return checked(response)["stream_id"]


def create_public_channel(client: zulip.Client, channel: str, user_id: int, bot_id: int) -> int:
    # Unlike the older subscribe endpoint, this endpoint rejects existing names
    # instead of adding users to an existing (potentially private) channel.
    try:
        response = client.call_endpoint(
            "channels/create",
            method="POST",
            request={
                "name": channel,
                "subscribers": [user_id, bot_id],
                "invite_only": False,
                "is_web_public": False,
                "announce": False,
                "can_administer_channel_group": {
                    "direct_members": [user_id, bot_id],
                    "direct_subgroups": [],
                },
            },
        )
    except requests.RequestException:
        raise ProjectError(
            "Could not confirm whether Zulip created the channel. Settings were not saved. "
            "Check the channel in Zulip, then repeat the original command."
        ) from None
    if response.get("code") == "CHANNEL_ALREADY_EXISTS":
        raise ProjectError(
            "That channel already exists. Settings were not saved. "
            "Check access and membership, then repeat the original command."
        )
    if response.get("result") != "success":
        raise ProjectError(
            "Zulip did not confirm channel creation. Settings were not saved. "
            "Check the channel name and whether AstroChat can create public channels and "
            "add members. You can create the channel manually, add yourself and AstroChat, "
            "then repeat the original command."
        )
    return response["id"]
