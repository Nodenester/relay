"""Discord plugin for relay.

Connects a bot to a single Discord channel. Incoming messages from the user
become events. Claude posts back via the `discord_send` tool.
"""
from __future__ import annotations

import logging

import discord

log = logging.getLogger("relay.plugin.discord")


MAX_DISCORD_MSG = 1990  # real limit 2000, leave headroom


async def setup(api) -> None:
    token = api.config.get("token")
    channel_id = int(api.config.get("channel_id", 0))

    if not token or token == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("discord.token not set in config.json")
    if not channel_id:
        raise ValueError("discord.channel_id not set in config.json")

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    ready = False

    @client.event
    async def on_ready():
        nonlocal ready
        ready = True
        log.info("Discord bot connected as %s", client.user)
        channel = client.get_channel(channel_id)
        if channel is None:
            log.warning("Channel %s not visible to bot — check permissions", channel_id)
        else:
            log.info("Watching channel: %s", getattr(channel, "name", channel_id))

    @client.event
    async def on_message(message: discord.Message):
        # Ignore self and other bots.
        if client.user is None or message.author.id == client.user.id:
            return
        if message.author.bot:
            return
        # Restrict to the configured channel.
        if message.channel.id != channel_id:
            return
        body = message.content
        if not body and not message.attachments:
            return
        if message.attachments:
            att_desc = "\n".join(
                f"(attachment: {a.filename}, {a.url})" for a in message.attachments
            )
            body = (body + "\n" + att_desc).strip()
        await api.emit(
            body=body,
            metadata={
                "channel": f"#{getattr(message.channel, 'name', channel_id)}",
                "from": message.author.display_name,
            },
        )

    @api.tool("Post a message in the watched Discord channel.")
    async def send(message: str) -> str:
        """Send a text message to the Discord channel the bot is watching.

        Args:
            message: the text to post (split automatically if over Discord's limit)
        """
        if not ready:
            return "ERROR: Discord bot not ready yet"
        channel = client.get_channel(channel_id)
        if channel is None:
            return f"ERROR: channel {channel_id} not reachable"
        if not message:
            return "ERROR: empty message"
        chunks = [message[i : i + MAX_DISCORD_MSG] for i in range(0, len(message), MAX_DISCORD_MSG)]
        for chunk in chunks:
            await channel.send(chunk)
        return f"sent ({len(chunks)} chunk{'s' if len(chunks) > 1 else ''}, {len(message)} chars)"

    # Launch the bot as a background task.
    api.spawn(client.start(token))
