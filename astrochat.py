class AstroChatBot(object):
    def usage(self):
        return """
            This plugin provides a simple chatbot for Zulip.
        """

    def handle_message(self, message, bot_handler):
        bot_handler.send_reply(message, "Hello! I'm AstroChat, your friendly chatbot. How can I assist you today?")


handler_class = AstroChatBot
