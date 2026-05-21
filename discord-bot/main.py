"""
main.py
-------
Entry point for the AutoRes Discord bot.

Commands (prefix: d!)
---------------------
d!autores          — Reply to a checklist message to parse and send all commands.
d!preview          — Reply to a checklist message to preview commands first.
d!setrole <role>   — Set which role is allowed to use ALL bot commands.
                     Requires Administrator permission.
d!help             — Show this help message (access-gated).

Access rules
------------
- ALL commands (including d!help) require either:
    • Administrator permission, OR
    • The role configured via d!setrole
- If no role has been set yet, only Administrators can use the bot.

Environment variables
---------------------
DISCORD_TOKEN   — Bot token (set in Render dashboard or local .env).
"""

import logging
import os

import discord
from discord.ext import commands

from keep_alive import keep_alive
from parser import parse_checklist
from queue_manager import QueueManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("autobot")

# ---------------------------------------------------------------------------
# Bot setup — disable the built-in help command so we can replace it with
# our own access-gated version.
# ---------------------------------------------------------------------------
BOT_PREFIX = "d!"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=BOT_PREFIX,
    intents=intents,
    help_command=None,  # Disable default help so we control access to it.
)

queue_manager = QueueManager()

# In-memory store: guild_id -> role_id of the configured allowed role.
allowed_roles: dict[int, int] = {}


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------

def is_allowed(ctx: commands.Context) -> bool:
    """
    Return True if the member may use any bot command.
    Granted when the member is an Administrator OR has the configured role.
    """
    member: discord.Member = ctx.author

    if member.guild_permissions.administrator:
        return True

    role_id = allowed_roles.get(ctx.guild.id)
    if role_id is not None:
        return any(r.id == role_id for r in member.roles)

    return False


def _access_denied_message(ctx: commands.Context) -> str:
    """Build a human-readable denial message for the invoking guild."""
    role_id = allowed_roles.get(ctx.guild.id)
    if role_id:
        role = ctx.guild.get_role(role_id)
        role_name = role.name if role else "the configured role"
        return f"You need the **{role_name}** role (or Administrator) to use this."
    return (
        "No allowed role has been set yet. "
        "An Administrator must run `d!setrole <role>` first."
    )


# ---------------------------------------------------------------------------
# Shared helper: fetch replied-to message content
# ---------------------------------------------------------------------------

def _clean_content(text: str) -> str:
    """
    Strip Discord markdown bold/italic markers (**text**, *text*) so the
    parser sees plain category and pokemon names even when the checklist was
    written with bold formatting.
    """
    # Remove bold (**) and italic (*) markers — do bold first (longer match).
    text = text.replace("**", "")
    text = text.replace("*", "")
    return text


async def get_replied_content(ctx: commands.Context) -> str | None:
    """
    Return the plain-text content of the message the user replied to.

    Handles three cases:
    1. Normal reply       — fetch the referenced message directly.
    2. Forwarded message  — the forwarded message's content lives in
                            message_snapshots, not in .content.
    3. Resolved reference — fall back to ref.resolved if fetch fails.
    """
    ref = ctx.message.reference
    if ref is None:
        await ctx.send("You must **reply** to a checklist message when using this command.")
        return None

    content = ""

    # Case 1 & 2: fetch the replied-to message then check message_snapshots.
    try:
        replied_msg = await ctx.channel.fetch_message(ref.message_id)
        content = replied_msg.content.strip()

        # Forwarded messages have empty .content — the real text is in
        # message_snapshots (discord.py 2.4+).
        if not content:
            snapshots = getattr(replied_msg, "message_snapshots", None)
            if snapshots:
                content = snapshots[0].message.content.strip()

    except (discord.NotFound, discord.HTTPException):
        pass

    # Case 3: fall back to the resolved reference object if we still have nothing.
    if not content and ref.resolved and hasattr(ref.resolved, "content"):
        content = ref.resolved.content.strip()

    if not content:
        await ctx.send(
            "Could not read the replied message. "
            "If it is a forwarded message, make sure the bot has permission to read "
            "the channel it was forwarded from."
        )
        return None

    return _clean_content(content)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Prefix: %s | Bot is ready.", BOT_PREFIX)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error("Unhandled command error: %s", error)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """Show all available commands. Access-gated."""
    if not is_allowed(ctx):
        await ctx.send(_access_denied_message(ctx))
        return

    role_id = allowed_roles.get(ctx.guild.id)
    if role_id:
        role = ctx.guild.get_role(role_id)
        access_line = f"Allowed role: **{role.name if role else role_id}** (or Administrator)"
    else:
        access_line = "Access: **Administrators only** (use `d!setrole` to open access)"

    embed = discord.Embed(
        title="AutoRes Bot — Commands",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="`d!autores`",
        value="Reply to a checklist message to parse it and send all `h!r` commands automatically.",
        inline=False,
    )
    embed.add_field(
        name="`d!preview`",
        value="Reply to a checklist message to see all commands that *would* be sent, without actually sending them.",
        inline=False,
    )
    embed.add_field(
        name="`d!setrole <role>`",
        value="Set which role can use all bot commands. **Requires Administrator.**",
        inline=False,
    )
    embed.add_field(
        name="`d!help`",
        value="Show this message.",
        inline=False,
    )
    embed.set_footer(text=access_line)
    await ctx.send(embed=embed)


@bot.command(name="setrole")
async def setrole(ctx: commands.Context, *, role_input: str = ""):
    """Set which role is allowed to use all bot commands. Admin only."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need the **Administrator** permission to use this.")
        return

    if not role_input.strip():
        await ctx.send("Please specify a role. Example: `d!setrole @Moderator`")
        return

    role: discord.Role | None = None

    try:
        role = await commands.RoleConverter().convert(ctx, role_input.strip())
    except commands.BadArgument:
        pass

    if role is None:
        needle = role_input.strip().lower()
        role = discord.utils.find(lambda r: r.name.lower() == needle, ctx.guild.roles)

    if role is None:
        await ctx.send(
            f"Could not find a role matching **{role_input}**. "
            "Try mentioning it directly (e.g. `d!setrole @Moderator`) or use its exact name."
        )
        return

    allowed_roles[ctx.guild.id] = role.id
    logger.info(
        "setrole: guild %s → role '%s' (%d) set by %s",
        ctx.guild.id, role.name, role.id, ctx.author,
    )
    await ctx.send(
        f"Done! Members with the **{role.name}** role can now use all bot commands."
    )


@bot.command(name="autores")
async def autores(ctx: commands.Context):
    """Parse a replied-to checklist and send all h!r commands one-by-one."""
    if not is_allowed(ctx):
        await ctx.send(_access_denied_message(ctx))
        return

    if queue_manager.is_busy(ctx.channel):
        await ctx.send(
            "An autores session is already running in this channel. "
            "Please wait for it to finish."
        )
        return

    content = await get_replied_content(ctx)
    if content is None:
        return

    logger.info("autores triggered by %s in #%s", ctx.author, ctx.channel.name)

    reserved_set: set[str] = set()
    commands_list = parse_checklist(content, reserved_set)

    if not commands_list:
        await ctx.send("No valid commands could be parsed from that checklist.")
        return

    await ctx.send(f"Starting autores — sending **{len(commands_list)}** command(s)...")
    logger.info("Queuing %d commands for #%s", len(commands_list), ctx.channel.name)

    try:
        queue_manager.start_session(ctx.channel, commands_list)
    except RuntimeError as exc:
        await ctx.send(str(exc))


@bot.command(name="preview")
async def preview(ctx: commands.Context):
    """Parse a replied-to checklist and show all commands WITHOUT sending them."""
    if not is_allowed(ctx):
        await ctx.send(_access_denied_message(ctx))
        return

    content = await get_replied_content(ctx)
    if content is None:
        return

    logger.info("preview triggered by %s in #%s", ctx.author, ctx.channel.name)

    reserved_set: set[str] = set()
    commands_list = parse_checklist(content, reserved_set)

    if not commands_list:
        await ctx.send("No valid commands could be parsed from that checklist.")
        return

    header = f"**Preview — {len(commands_list)} command(s) to send:**\n"
    lines = [f"`{i + 1}. {cmd}`" for i, cmd in enumerate(commands_list)]
    chunk_size = 1900

    full_message = header + "\n".join(lines)
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

    logger.info("Preview shown to %s: %d commands", ctx.author, len(commands_list))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    keep_alive()
    logger.info("Keep-alive server started.")

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is not set. "
            "Add it in your Render dashboard under Environment."
        )

    bot.run(token)
