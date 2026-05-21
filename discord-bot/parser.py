"""
parser.py
---------
Responsible for turning a raw checklist message into a flat list of
Discord commands that will later be queued and sent one-by-one.

Real-world checklist format (as used in production)
----------------------------------------------------
RARE #🔷| 1-rares to #🔷| 20 : @Able +cyndaquil
REGIONAL #🔷| 21-regionals to #🔷| 35 : @Dusky +ditto
EEVOS #🔷| 36-eevos to #🔷| 45 : @Able + missingno
GMAX #🔷| 46-gmax to #🔷| 55 : @Dusky + all urshifu
EVENT1 #🔷| 56-paradox to #🔷| 70 : @Able cicada vikavolt
EVENT2 #🔷| 71 to #🔷| 85 :
PARADOX #🔷| 86-res-1 to #🔷| 92 : @Dusky
RES1 #🔷| 93 to #🔷| 96-res-3 :
RES2 #🔷| 97 to #🔷| 100 : @Able poliwag | @Dusky all arceus

Key parsing insight
-------------------
- The CATEGORY NAME is always the very first word of the line.
- There is always a ':' that separates the channel/number info from the
  user+pokemon section. Everything AFTER THE LAST ':' is user+pokemon data.
- This safely ignores whatever text sits between the category and the colon.
"""

import re

# ---------------------------------------------------------------------------
# Category mapping: first word of line (lowercase) -> reserve slug.
#
# Categories with a slug   → command uses:  h!r add <slug>, <pokemon> @user
# Categories with None     → command uses:  h!r add <pokemon> @user  (no slug)
# ---------------------------------------------------------------------------
CATEGORY_MAP: dict[str, str | None] = {
    # --- Reserve categories (slug included in command) ---
    "rare":      "rare",
    "rares":     "rare",
    "regional":  "regional",
    "regionals": "regional",
    "eevo":      "eevo",
    "eevos":     "eevo",
    "gmax":      "gmax",
    "paradox":   "paradox",
    # --- Non-reserve categories (NO slug in command) ---
    "event1":    None,
    "event2":    None,
    "event3":    None,
    "res1":      None,
    "res2":      None,
    "res3":      None,
}

# ---------------------------------------------------------------------------
# Alias dictionary: multi-word Pokémon tokens that must NOT be split on
# spaces or '+'.  Keys are lowercase; values are sent verbatim in commands.
# Add new aliases here without touching any other code.
# ---------------------------------------------------------------------------
ALIASES: dict[str, str] = {
    "all urshifu":   "all urshifu",
    "all vivillon":  "all vivillon",
    "all arceus":    "all arceus",
    "all calyrex":   "all calyrex",
    # Add more multi-word aliases here as needed.
}

# Pre-compile a regex that matches any alias (longest first to prevent
# partial matches, e.g. match "all urshifu" before plain "urshifu").
_ALIAS_PATTERN: re.Pattern | None = re.compile(
    "|".join(
        re.escape(alias) for alias in sorted(ALIASES, key=len, reverse=True)
    ),
    re.IGNORECASE,
) if ALIASES else None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_pokemon_tokens(raw: str) -> list[str]:
    """
    Split a raw Pokémon string into individual tokens while correctly
    preserving multi-word aliases (e.g. "all urshifu").

    Splitting rules:
    - If '+' is present  → split on '+'.
    - If no '+' present  → split on whitespace (handles "cicada vikavolt").
    Aliases are protected from splitting via placeholder substitution.
    """
    raw = raw.strip()
    if not raw:
        return []

    # Step 1: Replace known aliases with opaque placeholders.
    placeholders: dict[str, str] = {}

    def _replace_alias(match: re.Match) -> str:
        key = f"__ALIAS_{len(placeholders)}__"
        placeholders[key] = ALIASES[match.group(0).lower()]
        return key

    if _ALIAS_PATTERN:
        raw = _ALIAS_PATTERN.sub(_replace_alias, raw)

    # Step 2: Split — prefer '+' as explicit separator, else use whitespace.
    if "+" in raw:
        parts = [p.strip() for p in raw.split("+") if p.strip()]
    else:
        parts = [p.strip() for p in raw.split() if p.strip()]

    # Step 3: Restore alias placeholders in each token.
    tokens: list[str] = []
    for part in parts:
        for key, alias_value in placeholders.items():
            part = part.replace(key, alias_value)
        token = part.strip()
        if token:
            tokens.append(token)

    return tokens


def _parse_user_segment(segment: str) -> tuple[str, list[str]]:
    """
    Parse one user segment such as "@mention +pokemon1 + pokemon2" or
    "@mention pokemon1 pokemon2" into (mention_string, [pokemon_tokens]).

    Returns ("", []) when no valid Discord mention is found.
    """
    segment = segment.strip()

    mention_match = re.search(r"<@!?\d+>", segment)
    if not mention_match:
        return "", []

    mention = mention_match.group(0)
    after_mention = segment[mention_match.end():].strip().lstrip("+").strip()

    pokemon_tokens = _extract_pokemon_tokens(after_mention) if after_mention else []
    return mention, pokemon_tokens


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_checklist(message_content: str, reserved_set: set[str]) -> list[str]:
    """
    Parse a full checklist message and return an ordered list of command
    strings ready to be sent in Discord.

    Parameters
    ----------
    message_content : str
        Raw text of the checklist message that was replied to.
    reserved_set : set[str]
        Mutable set of lowercase Pokémon names already reserved this session.
        Updated in-place so later lines can detect conflicts.

    Returns
    -------
    list[str]
        Ordered commands, e.g.:
        ["h!r remove eternatus", "h!r add eevo, eternatus @user3"]
    """
    commands: list[str] = []

    for raw_line in message_content.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # ------------------------------------------------------------------
        # Step 1: Extract category name — always the FIRST word of the line.
        # ------------------------------------------------------------------
        first_word = line.split()[0].lower().rstrip(":")

        if first_word not in CATEGORY_MAP:
            continue  # Not a recognised category; skip silently.

        category_slug = CATEGORY_MAP[first_word]  # str or None

        # ------------------------------------------------------------------
        # Step 2: Extract user+pokemon section — everything AFTER THE LAST
        # ':' in the line.  This skips over channel names / number ranges
        # that appear between the category name and the actual data.
        # ------------------------------------------------------------------
        if ":" not in line:
            continue

        user_section = line.rsplit(":", 1)[1].strip()

        if not user_section:
            continue  # Line has a category but no user/pokemon data; skip.

        # ------------------------------------------------------------------
        # Step 3: Split on '|' to handle multiple users on the same line
        # (e.g. RES2 ... : @user1 poliwag | @user2 all arceus).
        # NOTE: '|' characters inside the channel-info section (before ':')
        # are already excluded because we only look at user_section.
        # ------------------------------------------------------------------
        user_segments = [seg.strip() for seg in user_section.split("|")]

        for segment in user_segments:
            if not segment:
                continue

            # --------------------------------------------------------------
            # Step 4: Parse the mention and Pokémon list from this segment.
            # --------------------------------------------------------------
            mention, pokemon_tokens = _parse_user_segment(segment)

            if not mention or not pokemon_tokens:
                continue

            # --------------------------------------------------------------
            # Step 5: Insert 'h!r remove' commands where required.
            #
            # Rules:
            #   a) Any multi-word "all X" alias always gets a remove first
            #      (it represents a category-wide hold that must be freed).
            #   b) For eevo lines: if the Pokémon was already reserved
            #      earlier this session, remove it first before re-adding.
            # --------------------------------------------------------------
            for token in pokemon_tokens:
                token_lower = token.lower()
                is_alias = token_lower in ALIASES

                needs_remove = (
                    is_alias
                    or (category_slug == "eevo" and token_lower in reserved_set)
                )

                if needs_remove:
                    commands.append(f"h!r remove {token}")

            # --------------------------------------------------------------
            # Step 6: Generate the add command.
            #
            #   With category slug:  h!r add <slug>, p1, p2 @mention
            #   Without slug:        h!r add p1, p2 @mention
            # --------------------------------------------------------------
            pokemon_list_str = ", ".join(pokemon_tokens)

            if category_slug:
                commands.append(f"h!r add {category_slug}, {pokemon_list_str} {mention}")
            else:
                commands.append(f"h!r add {pokemon_list_str} {mention}")

            # --------------------------------------------------------------
            # Step 7: Mark these Pokémon as reserved for conflict detection
            # on later lines in the same session.
            # --------------------------------------------------------------
            for token in pokemon_tokens:
                reserved_set.add(token.lower())

    return commands
