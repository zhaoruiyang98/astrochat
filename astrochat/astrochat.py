from typing import Any
from zulip_bots.lib import AbstractBotHandler


class AstroChatBot(object):
    def usage(self) -> str:
        return """
            This plugin provides a simple chatbot for Zulip.
        """

    def handle_message(self, message: dict[str, Any], bot_handler: AbstractBotHandler) -> None:
        bot_handler.send_reply(message, "Hello! I'm AstroChat, your friendly chatbot. How can I assist you today?")

        bot_handler.react(message, 'wave')


handler_class = AstroChatBot
