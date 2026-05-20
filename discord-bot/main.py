"""
main.py
-------
Entry point for the AutoRes Discord bot.

The bot reads manually-written Pokétwo checklist messages (via a reply) and
automatically dispatches h!r add / h!r remove commands to the channel,
one-by-one with a short delay between them.

Commands (prefix: d!)
---------------------
d!autores   — Reply to a checklist message to parse and send all commands.
d!preview   — Reply to a checklist message to see commands WITHOUT sending.

Permissions required: Manage Messages (checked before every command).

Environment variables
---------------------
DISCORD_TOKEN   — Bot token from the Discord developer portal.
                  Set this in your Render dashboard (or a local .env file).
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands

from keep_alive import keep_alive
from parser import parse_checklist
from queue_manager import QueueManager

# ---------------------------------------------------------------------------
# Logging setup — clean, timestamped output visible in Render's log viewer.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("autobot")

# ---------------------------------------------------------------------------
# Bot configuration
# ---------------------------------------------------------------------------
BOT_PREFIX = "d!"

intents = discord.Intents.default()
intents.message_content = True  # Required to read message text.

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# One QueueManager for the whole bot — it tracks active sessions per channel.
queue_manager = QueueManager()


# ---------------------------------------------------------------------------
# Helper: permission check
# ---------------------------------------------------------------------------
def has_manage_messages(ctx: commands.Context) -> bool:
    """Return True if the invoking member has the Manage Messages permission."""
    return ctx.author.guild_permissions.manage_messages


# ---------------------------------------------------------------------------
# Helper: extract checklist text from replied-to message
# ---------------------------------------------------------------------------
async def get_replied_content(ctx: commands.Context) -> str | None:
    """
    Return the text content of the message the user replied to, or None if
    the invocation was not a reply or the referenced message has no text.
    """
    ref = ctx.message.reference
    if ref is None:
        await ctx.send(
            "You must **reply** to a checklist message when using this command."
        )
        return None

    # Fetch the referenced message (it may not be cached).
    try:
        replied_msg = await ctx.channel.fetch_message(ref.message_id)
    except discord.NotFound:
        await ctx.send("Could not find the message you replied to.")
        return None

    content = replied_msg.content.strip()
    if not content:
        await ctx.send("The replied message appears to be empty.")
        return None

    return content


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Prefix: %s", BOT_PREFIX)
    logger.info("Bot is ready and waiting for commands.")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Global error handler — log unexpected errors without crashing."""
    if isinstance(error, commands.CommandNotFound):
        return  # Silently ignore unknown prefixed messages.
    logger.error("Unhandled command error: %s", error)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.command(name="autores")
async def autores(ctx: commands.Context):
    """
    Parse a replied-to checklist and send all generated commands to the
    channel one-by-one with a 2-second delay between each.

    Usage:  Reply to a checklist message, then type  d!autores
    """
    # --- Permission guard ---
    if not has_manage_messages(ctx):
        await ctx.send("You need the **Manage Messages** permission to use this.")
        return

    # --- Anti-spam: only one session per channel at a time ---
    if queue_manager.is_busy(ctx.channel):
        await ctx.send(
            "An autores session is already running in this channel. "
            "Please wait for it to finish."
        )
        return

    # --- Fetch the checklist text ---
    content = await get_replied_content(ctx)
    if content is None:
        return

    logger.info(
        "autores triggered by %s in #%s", ctx.author, ctx.channel.name
    )

    # --- Parse the checklist into commands ---
    # reserved_set is per-session; it lets later lines detect conflicts with
    # Pokémon that were reserved by earlier lines in the same checklist.
    reserved_set: set[str] = set()
    commands_list = parse_checklist(content, reserved_set)

    if not commands_list:
        await ctx.send("No valid commands could be parsed from that checklist.")
        return

    await ctx.send(
        f"Starting autores — sending **{len(commands_list)}** command(s)..."
    )
    logger.info("Queuing %d commands for #%s", len(commands_list), ctx.channel.name)

    # --- Hand off to the queue manager ---
    try:
        queue_manager.start_session(ctx.channel, commands_list)
    except RuntimeError as exc:
        await ctx.send(str(exc))


@bot.command(name="preview")
async def preview(ctx: commands.Context):
    """
    Parse a replied-to checklist and display all commands that WOULD be sent,
    without actually sending them.

    Usage:  Reply to a checklist message, then type  d!preview
    """
    # --- Permission guard ---
    if not has_manage_messages(ctx):
        await ctx.send("You need the **Manage Messages** permission to use this.")
        return

    # --- Fetch the checklist text ---
    content = await get_replied_content(ctx)
    if content is None:
        return

    logger.info(
        "preview triggered by %s in #%s", ctx.author, ctx.channel.name
    )

    # --- Parse without affecting reserved_set (use a fresh one) ---
    reserved_set: set[str] = set()
    commands_list = parse_checklist(content, reserved_set)

    if not commands_list:
        await ctx.send("No valid commands could be parsed from that checklist.")
        return

    # --- Format the preview message ---
    # Discord messages have a 2000 character limit, so chunk if needed.
    header = f"**Preview — {len(commands_list)} command(s) to send:**\n"
    lines = [f"`{i + 1}. {cmd}`" for i, cmd in enumerate(commands_list)]
    body = "\n".join(lines)

    full_message = header + body

    # Send in chunks if the message would exceed Discord's 2000-char limit.
    chunk_size = 1900
    if len(full_message) <= chunk_size:
        await ctx.send(full_message)
    else:
        await ctx.send(header)
        chunk: list[str] = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > chunk_size:
                await ctx.send("\n".join(chunk))
                chunk = []
                current_len = 0
            chunk.append(line)
            current_len += len(line) + 1
        if chunk:
            await ctx.send("\n".join(chunk))

    logger.info(
        "Preview shown to %s: %d commands", ctx.author, len(commands_list)
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Start the Flask keep-alive server in a background thread so Render
    # doesn't spin down the container due to inactivity.
    keep_alive()
    logger.info("Keep-alive server started.")

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is not set. "
            "Add it in your Render dashboard under Environment."
        )

    bot.run(token)
