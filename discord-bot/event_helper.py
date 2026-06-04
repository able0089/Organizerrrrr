"""
event_helper.py
---------------
Tracks Pokétwo event quests per user across multiple guilds.

How it works
------------
1. A user runs d!evhelp — they are registered as "tracking active".
2. Whenever Pokétwo sends a reply to that user containing an event quest embed,
   this module parses the quests and updates/creates a persistent tracker message.
3. When all quests complete the user is pinged once and tracking waits for the
   next quest list.
4. State is saved to event_data.json so trackers survive bot restarts.

Quest embed format (from Pokétwo)
----------------------------------
The embed description contains lines like:
    **2**. Open 2 Supply Crates 0/2
    **3**. Catch 6 Ground-type pokémon 0/6
Progress is always "current/max" at the end of each line.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

import discord

logger = logging.getLogger("event_helper")

SAVE_FILE = os.path.join(os.path.dirname(__file__), "event_data.json")

# Matches quest lines: optional bold markers, quest number, period, description,
# then the progress "current/max" at the end.
# Examples:
#   **2**. Open 2 Supply Crates 0/2
#   3. Catch 6 Ground-type pokémon 0/6
#   4. Catch 7 Ice-type pokémon 4/7
QUEST_RE = re.compile(
    r"\*{0,2}(\d+)\*{0,2}\.\s+(.+?)\s+(\d+)/(\d+)\b",
    re.MULTILINE,
)

# Keywords that identify this embed as a quest embed (at least one must match).
QUEST_KEYWORDS = ("active quest", "your quest", "quest", "ev q", "event quest")


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _make_tracker() -> dict:
    return {
        "guild_id": 0,
        "user_id": 0,
        "channel_id": 0,        # Channel where d!evhelp was run (tracker lives here)
        "tracker_msg_id": None, # ID of the persistent tracker message
        "active": False,
        "quests": [],           # list of quest dicts
        "all_completed_notified": False,
    }


def _quest(number: int, description: str, current: int, maximum: int) -> dict:
    return {
        "number": number,
        "description": description,
        "current": current,
        "maximum": maximum,
        "completed": current >= maximum,
    }


# ---------------------------------------------------------------------------
# EventHelper
# ---------------------------------------------------------------------------

class EventHelper:
    """Manages per-user quest trackers across all guilds."""

    def __init__(self) -> None:
        # (guild_id, user_id) → tracker dict
        self._trackers: dict[tuple[int, int], dict] = {}
        # Set of (guild_id, user_id) with active tracking
        self._active: set[tuple[int, int]] = set()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        try:
            payload = []
            for (guild_id, user_id), state in self._trackers.items():
                payload.append(dict(state, guild_id=guild_id, user_id=user_id))
            with open(SAVE_FILE, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except Exception as exc:
            logger.error("Failed to save event data: %s", exc)

    def _load(self) -> None:
        if not os.path.exists(SAVE_FILE):
            return
        try:
            with open(SAVE_FILE, encoding="utf-8") as fh:
                payload = json.load(fh)
            for entry in payload:
                guild_id = int(entry["guild_id"])
                user_id  = int(entry["user_id"])
                key = (guild_id, user_id)
                self._trackers[key] = {
                    "guild_id":               guild_id,
                    "user_id":                user_id,
                    "channel_id":             int(entry.get("channel_id", 0)),
                    "tracker_msg_id":         entry.get("tracker_msg_id"),
                    "active":                 bool(entry.get("active", False)),
                    "quests":                 entry.get("quests", []),
                    "all_completed_notified": bool(entry.get("all_completed_notified", False)),
                }
                if entry.get("active"):
                    self._active.add(key)
            logger.info("Loaded %d event trackers from disk.", len(self._trackers))
        except Exception as exc:
            logger.error("Failed to load event data: %s", exc)

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def enable(self, guild_id: int, user_id: int, channel_id: int) -> bool:
        """
        Register a user for quest tracking.
        Returns True if newly enabled, False if already active.
        """
        key = (guild_id, user_id)
        if key in self._active:
            return False
        self._active.add(key)
        if key not in self._trackers:
            state = _make_tracker()
            state.update(guild_id=guild_id, user_id=user_id,
                         channel_id=channel_id, active=True)
            self._trackers[key] = state
        else:
            self._trackers[key]["active"] = True
            self._trackers[key]["channel_id"] = channel_id
        self._save()
        logger.info("evhelp enabled: user=%d guild=%d", user_id, guild_id)
        return True

    def disable(self, guild_id: int, user_id: int) -> bool:
        """Stop tracking a user. Returns True if was active."""
        key = (guild_id, user_id)
        if key not in self._active:
            return False
        self._active.discard(key)
        if key in self._trackers:
            self._trackers[key]["active"] = False
        self._save()
        logger.info("evhelp disabled: user=%d guild=%d", user_id, guild_id)
        return True

    def is_active(self, guild_id: int, user_id: int) -> bool:
        return (guild_id, user_id) in self._active

    def get_state(self, guild_id: int, user_id: int) -> dict | None:
        return self._trackers.get((guild_id, user_id))

    # ------------------------------------------------------------------
    # Quest parsing
    # ------------------------------------------------------------------

    def _is_quest_embed(self, embed: discord.Embed) -> bool:
        """Return True if the embed looks like a Pokétwo quest embed."""
        text = " ".join(filter(None, [
            embed.title or "",
            embed.description or "",
            *(f.name or "" for f in embed.fields),
            *(f.value or "" for f in embed.fields),
        ])).lower()
        return any(kw in text for kw in QUEST_KEYWORDS)

    def _parse_quests(self, embed: discord.Embed) -> list[dict] | None:
        """
        Extract quest list from a Pokétwo event embed.
        Returns a list of quest dicts, or None if no quests are found.
        """
        if not self._is_quest_embed(embed):
            return None

        # Gather all text from the embed
        full_text = "\n".join(filter(None, [
            embed.description or "",
            *(f.name or "" for f in embed.fields),
            *(f.value or "" for f in embed.fields),
        ]))

        matches = QUEST_RE.findall(full_text)
        if not matches:
            return None

        quests: list[dict] = []
        seen: set[int] = set()
        for num_str, desc, cur_str, max_str in matches:
            num = int(num_str)
            if num in seen:
                continue
            seen.add(num)
            quests.append(_quest(num, desc.strip(), int(cur_str), int(max_str)))

        return quests or None

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _bar(current: int, maximum: int, length: int = 10) -> str:
        if maximum == 0:
            return "░" * length
        filled = min(round(current / maximum * length), length)
        return "█" * filled + "░" * (length - filled)

    def _format(self, state: dict, member: discord.Member | None) -> str:
        mention = member.mention if member else f"<@{state['user_id']}>"
        now = datetime.now().strftime("%H:%M")
        lines = [f"🎯 **Quest Tracker — {mention}**", f"-# Updated {now}", ""]

        quests: list[dict] = state["quests"]
        if not quests:
            lines.append("*No quests loaded yet.*")
            lines.append(f"*Run `@Pokétwo ev q` to load your quests.*")
        else:
            for q in quests:
                icon = "✅" if q["completed"] else "⬜"
                bar  = self._bar(q["current"], q["maximum"])
                lines.append(
                    f"{icon} **Quest {q['number']}**: {q['description']}\n"
                    f"　　`{q['current']}/{q['maximum']}` {bar}"
                )

        if quests and all(q["completed"] for q in quests):
            lines += ["", "🎉 **All quests completed!**"]

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tracker message create / edit
    # ------------------------------------------------------------------

    async def _push_tracker(
        self,
        guild: discord.Guild,
        state: dict,
        fallback_channel: discord.TextChannel,
    ) -> None:
        """Create or edit the persistent tracker message."""
        member = guild.get_member(state["user_id"])
        content = self._format(state, member)

        channel = guild.get_channel(state["channel_id"]) or fallback_channel

        # Try editing the existing tracker message first.
        if state["tracker_msg_id"]:
            try:
                msg = await channel.fetch_message(state["tracker_msg_id"])
                await msg.edit(content=content)
                logger.info("Tracker updated for user %d", state["user_id"])
                return
            except (discord.NotFound, discord.HTTPException):
                state["tracker_msg_id"] = None
                logger.info("Old tracker gone, recreating for user %d", state["user_id"])

        # Create a new tracker message.
        try:
            msg = await channel.send(content)
            state["tracker_msg_id"] = msg.id
            logger.info("Tracker created for user %d (msg %d)", state["user_id"], msg.id)
        except discord.HTTPException as exc:
            logger.error("Could not create tracker for user %d: %s", state["user_id"], exc)

    # ------------------------------------------------------------------
    # Core entry point: called from on_message
    # ------------------------------------------------------------------

    async def handle_poketwo_reply(
        self,
        message: discord.Message,
        referenced_author_id: int,
    ) -> None:
        """
        Call this when Pokétwo sends a reply to a guild message.
        If the original author has evhelp active and the reply contains
        quest data, the tracker is updated.
        """
        guild = message.guild
        key = (guild.id, referenced_author_id)

        if key not in self._active:
            return  # That user isn't tracking

        state = self._trackers.get(key)
        if state is None:
            return

        # Parse quests from the embed(s)
        new_quests: list[dict] | None = None
        for embed in message.embeds:
            parsed = self._parse_quests(embed)
            if parsed is not None:
                new_quests = parsed
                break

        if new_quests is None:
            return  # Not a quest embed

        logger.info(
            "Quest embed for user %d in guild %d: %d quests found",
            referenced_author_id, guild.id, len(new_quests),
        )

        # Decide whether this is a fresh quest cycle or a progress update.
        old_numbers = {q["number"] for q in state["quests"]}
        new_numbers = {q["number"] for q in new_quests}

        if old_numbers != new_numbers:
            # Different quest numbers → new cycle, replace entirely.
            state["quests"] = new_quests
            state["all_completed_notified"] = False
            logger.info("New quest cycle for user %d", referenced_author_id)
        else:
            # Same quest numbers → update progress (never regress a completed quest).
            existing = {q["number"]: q for q in state["quests"]}
            for q in new_quests:
                prev = existing.get(q["number"])
                if prev:
                    q["current"] = max(q["current"], prev["current"])
                q["completed"] = q["current"] >= q["maximum"]
            state["quests"] = new_quests

        all_done = bool(state["quests"]) and all(q["completed"] for q in state["quests"])

        await self._push_tracker(guild, state, message.channel)

        if all_done and not state["all_completed_notified"]:
            state["all_completed_notified"] = True
            await self._notify_complete(guild, state, message.channel)

        self._save()

    async def _notify_complete(
        self,
        guild: discord.Guild,
        state: dict,
        fallback_channel: discord.TextChannel,
    ) -> None:
        """Ping the user when all quests are done."""
        member = guild.get_member(state["user_id"])
        mention = member.mention if member else f"<@{state['user_id']}>"
        channel = guild.get_channel(state["channel_id"]) or fallback_channel
        try:
            await channel.send(
                f"🎉 {mention} All your event quests are completed! "
                "Run `@Pokétwo ev q` again when you get new quests."
            )
            logger.info("Completion notification sent for user %d", state["user_id"])
        except discord.HTTPException as exc:
            logger.error("Failed to send completion notification: %s", exc)
