"""
main.py
-------
Entry point for the AutoRes Discord bot.

Commands (prefix: d!)
---------------------
d!autores                    — Reply to a checklist message to parse and send all commands.
d!preview                    — Reply to a checklist message to preview commands first.
d!setrole <role>             — Set which role can use all bot commands (Admin only).
d!set category <name/ID>     — Register a Discord category for pause/resume (Admin only).
d!p                          — Pause Pokétwo in the current channel.
d!p all                      — Pause Pokétwo in every channel in the registered category.
d!r                          — Resume Pokétwo in the current channel.
d!r all                      — Resume Pokétwo in every channel in the registered category.
d!clearres                   — Wipe the server's reserved-pokemon memory.
d!help                       — Show this message (access-gated).

Auto-pause
----------
When Pokétwo sends an incense-purchase message in a channel that belongs to the
registered category, the bot automatically pauses that channel.

Access rules
------------
- ALL commands require Administrator OR the role set via d!setrole.
- d!set category and d!setrole additionally require Administrator.

Environment variables
---------------------
DISCORD_TOKEN   — Bot token (Render dashboard or local .env).
"""

import logging
import os
import re

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
# Constants
# ---------------------------------------------------------------------------
BOT_PREFIX  = "d!"
POKETWO_ID  = 716390085896962058   # Pokétwo's Discord user/bot ID

# Detects any incense-purchase message sent by Pokétwo.
INCENSE_PATTERN = re.compile(
    r"(?:purchased|bought)\s+an?\s+incense"
    r"|incense\s+(?:purchased|bought|activated)"
    r"|you\s+purchased\s+an?\s+incense",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=BOT_PREFIX,
    intents=intents,
    help_command=None,   # Replaced with our own access-gated version.
)

queue_manager = QueueManager()

# guild_id -> role_id  (role allowed to use all bot commands)
allowed_roles: dict[int, int] = {}

# guild_id -> category_id  (registered Discord category for pause/resume)
guild_categories: dict[int, int] = {}

# guild_id -> set of lowercase pokemon names reserved this cycle
guild_reserved: dict[int, set[str]] = {}


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------

def is_allowed(ctx: commands.Context) -> bool:
    """Administrator OR member with the configured allowed role."""
    member: discord.Member = ctx.author
    if member.guild_permissions.administrator:
        return True
    role_id = allowed_roles.get(ctx.guild.id)
    if role_id is not None:
        return any(r.id == role_id for r in member.roles)
    return False


def _access_denied_message(ctx: commands.Context) -> str:
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
# Pause / resume helpers
# ---------------------------------------------------------------------------

async def _get_poketwo(guild: discord.Guild) -> discord.Member | None:
    """Return the Pokétwo member object, fetching from API if not cached."""
    member = guild.get_member(POKETWO_ID)
    if member is None:
        try:
            member = await guild.fetch_member(POKETWO_ID)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            return None
    return member


async def _pause_channel(
    channel: discord.TextChannel,
    poketwo: discord.Member,
) -> bool:
    """
    Deny Send Messages + View Channel for Pokétwo in one channel.
    Returns True on success, False on permission error.
    """
    try:
        await channel.set_permissions(
            poketwo,
            send_messages=False,
            view_channel=False,
            reason="AutoRes: incense pause",
        )
        logger.info("Paused Pokétwo in #%s", channel.name)
        return True
    except discord.Forbidden:
        logger.warning("Missing permissions to pause #%s", channel.name)
        return False
    except discord.HTTPException as exc:
        logger.warning("HTTP error pausing #%s: %s", channel.name, exc)
        return False


async def _resume_channel(
    channel: discord.TextChannel,
    poketwo: discord.Member,
) -> bool:
    """
    Explicitly ALLOW Send Messages + View Channel for Pokétwo in one channel.
    This sets a green checkmark override rather than just removing the deny,
    ensuring Pokétwo can post even if a category-level deny exists.
    Returns True on success, False on permission error.
    """
    try:
        await channel.set_permissions(
            poketwo,
            send_messages=True,
            view_channel=True,
            reason="AutoRes: incense resume",
        )
        logger.info("Resumed Pokétwo in #%s", channel.name)
        return True
    except discord.Forbidden:
        logger.warning("Missing permissions to resume #%s", channel.name)
        return False
    except discord.HTTPException as exc:
        logger.warning("HTTP error resuming #%s: %s", channel.name, exc)
        return False


def _registered_text_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    """Return all text channels inside the guild's registered category."""
    cat_id = guild_categories.get(guild.id)
    if cat_id is None:
        return []
    category = guild.get_channel(cat_id)
    if not isinstance(category, discord.CategoryChannel):
        return []
    return [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]


# ---------------------------------------------------------------------------
# Shared helper: fetch replied-to message content
# ---------------------------------------------------------------------------

def _clean_content(text: str) -> str:
    """Strip Discord bold (**) and italic (*) markers from text."""
    text = text.replace("**", "")
    text = text.replace("*", "")
    return text


async def get_replied_content(ctx: commands.Context) -> str | None:
    """
    Return plain-text content of the message the user replied to.
    Handles normal replies, forwarded messages (message_snapshots), and
    resolved references.
    """
    ref = ctx.message.reference
    if ref is None:
        await ctx.send("You must **reply** to a checklist message when using this command.")
        return None

    content = ""

    try:
        replied_msg = await ctx.channel.fetch_message(ref.message_id)
        content = replied_msg.content.strip()
        if not content:
            snapshots = getattr(replied_msg, "message_snapshots", None)
            if snapshots:
                content = snapshots[0].message.content.strip()
    except (discord.NotFound, discord.HTTPException):
        pass

    if not content and ref.resolved and hasattr(ref.resolved, "content"):
        content = ref.resolved.content.strip()

    if not content:
        await ctx.send(
            "Could not read the replied message. "
            "If it is a forwarded message, make sure the bot has permission to "
            "read the channel it was forwarded from."
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


@bot.event
async def on_message(message: discord.Message):
    """
    Auto-pause: when Pokétwo announces an incense purchase in a channel that
    belongs to the registered category, immediately pause that channel.
    """
    # Let command processing run first.
    await bot.process_commands(message)

    # Only act on Pokétwo messages in a guild.
    if message.author.id != POKETWO_ID:
        return
    if message.guild is None:
        return

    # Only in channels that belong to the registered category.
    registered = _registered_text_channels(message.guild)
    if message.channel not in registered:
        return

    # Check for incense purchase keywords.
    if not INCENSE_PATTERN.search(message.content):
        # Also check embeds (Pokétwo sometimes uses embeds).
        embed_text = " ".join(
            (e.title or "") + " " + (e.description or "")
            for e in message.embeds
        )
        if not INCENSE_PATTERN.search(embed_text):
            return

    logger.info(
        "Incense purchase detected in #%s — auto-pausing Pokétwo.",
        message.channel.name,
    )

    poketwo = await _get_poketwo(message.guild)
    if poketwo is None:
        logger.warning("Pokétwo member not found in guild %s", message.guild.id)
        return

    success = await _pause_channel(message.channel, poketwo)
    if success:
        await message.channel.send("⏸️ Channel paused.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """Show all available commands."""
    if not is_allowed(ctx):
        await ctx.send(_access_denied_message(ctx))
        return

    role_id = allowed_roles.get(ctx.guild.id)
    if role_id:
        role = ctx.guild.get_role(role_id)
        access_line = f"Allowed role: **{role.name if role else role_id}** (or Administrator)"
    else:
        access_line = "Access: **Administrators only** (use `d!setrole` to open access)"

    cat_id = guild_categories.get(ctx.guild.id)
    if cat_id:
        cat = ctx.guild.get_channel(cat_id)
        cat_line = f"Registered category: **{cat.name if cat else cat_id}**"
    else:
        cat_line = "No category registered yet — use `d!set category <name/ID>`"

    embed = discord.Embed(title="AutoRes Bot — Commands", color=discord.Color.blurple())
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
        name="`d!p`",
        value="Pause Pokétwo in **this channel** (deny Send Messages + View Channel).",
        inline=False,
    )
    embed.add_field(
        name="`d!p all`",
        value="Pause Pokétwo in **every channel** in the registered category.",
        inline=False,
    )
    embed.add_field(
        name="`d!r`",
        value="Resume Pokétwo in **this channel**.",
        inline=False,
    )
    embed.add_field(
        name="`d!r all`",
        value="Resume Pokétwo in **every channel** in the registered category.",
        inline=False,
    )
    embed.add_field(
        name="`d!set category <name or ID>`",
        value="Register a Discord category for `d!p all` / `d!r all` and auto-pause. **Requires Administrator.**",
        inline=False,
    )
    embed.add_field(
        name="`d!setrole <role>`",
        value="Set which role can use all bot commands. **Requires Administrator.**",
        inline=False,
    )
    embed.add_field(
        name="`d!clearres`",
        value="Wipe the server's reserved-pokemon memory. Run at the start of a new reserve cycle.",
        inline=False,
    )
    embed.add_field(name="`d!help`", value="Show this message.", inline=False)
    embed.set_footer(text=f"{access_line} | {cat_line}")
    await ctx.send(embed=embed)


# --- d!set group -----------------------------------------------------------

@bot.group(name="set", invoke_without_command=True)
async def set_group(ctx: commands.Context):
    """Command group for bot configuration (Admin only)."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need the **Administrator** permission to use this.")
        return
    await ctx.send(
        "Available subcommands: `d!set category <name or ID>`"
    )


@set_group.command(name="category")
async def set_category(ctx: commands.Context, *, category_input: str = ""):
    """Register a Discord category channel for pause/resume operations."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need the **Administrator** permission to use this.")
        return

    if not category_input.strip():
        await ctx.send(
            "Please provide a category name or ID. "
            "Example: `d!set category Pokétwo Channels`"
        )
        return

    # Try to match by ID first, then by name (case-insensitive).
    category: discord.CategoryChannel | None = None

    if category_input.strip().isdigit():
        ch = ctx.guild.get_channel(int(category_input.strip()))
        if isinstance(ch, discord.CategoryChannel):
            category = ch

    if category is None:
        needle = category_input.strip().lower()
        for ch in ctx.guild.categories:
            if ch.name.lower() == needle:
                category = ch
                break

    if category is None:
        await ctx.send(
            f"Could not find a category matching **{category_input}**. "
            "Make sure you're using the exact category name or its ID."
        )
        return

    guild_categories[ctx.guild.id] = category.id
    channel_count = len(
        [c for c in category.channels if isinstance(c, discord.TextChannel)]
    )
    logger.info(
        "set category: guild %s → category '%s' (%d) with %d text channels, set by %s",
        ctx.guild.id, category.name, category.id, channel_count, ctx.author,
    )
    await ctx.send(
        f"Done! Registered **{category.name}** ({channel_count} text channels) "
        f"for pause/resume operations."
    )


# --- d!setrole -------------------------------------------------------------

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


# --- d!p (pause) -----------------------------------------------------------

@bot.command(name="p")
async def pause_cmd(ctx: commands.Context, *, arg: str = ""):
    """
    d!p       — Pause Pokétwo in this channel.
    d!p all   — Pause Pokétwo in every channel of the registered category.
    """
    if not is_allowed(ctx):
        await ctx.send(_access_denied_message(ctx))
        return

    poketwo = await _get_poketwo(ctx.guild)
    if poketwo is None:
        await ctx.send(
            "Could not find Pokétwo in this server. "
            "Make sure Pokétwo is a member of the server."
        )
        return

    if arg.strip().lower() == "all":
        # Pause all channels in the registered category.
        channels = _registered_text_channels(ctx.guild)
        if not channels:
            cat_id = guild_categories.get(ctx.guild.id)
            if cat_id is None:
                await ctx.send(
                    "No category has been registered yet. "
                    "An Administrator must run `d!set category <name/ID>` first."
                )
            else:
                await ctx.send("The registered category has no text channels.")
            return

        await ctx.send(f"⏸️ Pausing Pokétwo in **{len(channels)}** channel(s)...")
        ok, fail = 0, 0
        for ch in channels:
            if await _pause_channel(ch, poketwo):
                ok += 1
            else:
                fail += 1

        summary = f"⏸️ Done — paused **{ok}** channel(s)."
        if fail:
            summary += f" Failed on **{fail}** channel(s) (missing permissions)."
        await ctx.send(summary)

    else:
        # Pause only the current channel.
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command can only be used in a text channel.")
            return
        success = await _pause_channel(ctx.channel, poketwo)
        if success:
            await ctx.send(
                f"⏸️ Pokétwo has been paused in {ctx.channel.mention}.\n"
                f"Use `{BOT_PREFIX}r` to resume."
            )
        else:
            await ctx.send(
                "Failed to pause Pokétwo here. "
                "Make sure the bot has **Manage Channel** permissions."
            )


# --- d!r (resume) ----------------------------------------------------------

@bot.command(name="r")
async def resume_cmd(ctx: commands.Context, *, arg: str = ""):
    """
    d!r       — Resume Pokétwo in this channel.
    d!r all   — Resume Pokétwo in every channel of the registered category.
    """
    if not is_allowed(ctx):
        await ctx.send(_access_denied_message(ctx))
        return

    poketwo = await _get_poketwo(ctx.guild)
    if poketwo is None:
        await ctx.send(
            "Could not find Pokétwo in this server. "
            "Make sure Pokétwo is a member of the server."
        )
        return

    if arg.strip().lower() == "all":
        # Resume all channels in the registered category.
        channels = _registered_text_channels(ctx.guild)
        if not channels:
            cat_id = guild_categories.get(ctx.guild.id)
            if cat_id is None:
                await ctx.send(
                    "No category has been registered yet. "
                    "An Administrator must run `d!set category <name/ID>` first."
                )
            else:
                await ctx.send("The registered category has no text channels.")
            return

        await ctx.send(f"▶️ Resuming Pokétwo in **{len(channels)}** channel(s)...")
        ok, fail = 0, 0
        for ch in channels:
            if await _resume_channel(ch, poketwo):
                ok += 1
            else:
                fail += 1

        summary = f"▶️ Done — resumed **{ok}** channel(s)."
        if fail:
            summary += f" Failed on **{fail}** channel(s) (missing permissions)."
        await ctx.send(summary)

    else:
        # Resume only the current channel.
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command can only be used in a text channel.")
            return
        success = await _resume_channel(ctx.channel, poketwo)
        if success:
            await ctx.send(f"▶️ Pokétwo has been resumed in {ctx.channel.mention}.")
        else:
            await ctx.send(
                "Failed to resume Pokétwo here. "
                "Make sure the bot has **Manage Channel** permissions."
            )


# --- d!ping ----------------------------------------------------------------

@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    """Check the bot's latency."""
    if not is_allowed(ctx):
        await ctx.send(_access_denied_message(ctx))
        return
    latency_ms = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: **{latency_ms} ms**")


# --- d!clearres ------------------------------------------------------------

@bot.command(name="clearres")
async def clearres(ctx: commands.Context):
    """Wipe the server's persistent reserved-pokemon memory."""
    if not is_allowed(ctx):
        await ctx.send(_access_denied_message(ctx))
        return

    count = len(guild_reserved.get(ctx.guild.id, set()))
    guild_reserved[ctx.guild.id] = set()
    logger.info(
        "clearres: guild %s wiped %d reserved entries by %s",
        ctx.guild.id, count, ctx.author,
    )
    await ctx.send(f"Done! Reserved-pokemon memory cleared ({count} entries removed).")


# --- d!autores -------------------------------------------------------------

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

    if ctx.guild.id not in guild_reserved:
        guild_reserved[ctx.guild.id] = set()
    reserved_set = guild_reserved[ctx.guild.id]

    commands_list = parse_checklist(content, reserved_set)

    if not commands_list:
        await ctx.send("No valid commands could be parsed from that checklist.")
        return

    await ctx.send(f"Starting autores — sending **{len(commands_list)}** command(s)...")
    logger.info(
        "Queuing %d commands for #%s (guild reserved pool: %d pokemon)",
        len(commands_list), ctx.channel.name, len(reserved_set),
    )

    try:
        queue_manager.start_session(ctx.channel, commands_list)
    except RuntimeError as exc:
        await ctx.send(str(exc))


# --- d!preview -------------------------------------------------------------

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
