"""
parser.py
---------
Responsible for turning a raw checklist message into a flat list of
Discord commands that will later be queued and sent one-by-one.

Checklist format example
------------------------
Rares : @user + sewaddle
Regionals : @user2 + charmander
Eevos : @user3 + eternatus
Gmax : @user4 + applin + all urshifu
Res1 : @user5 popplio + paras
Res2 : @user6 bulbasaur | @user7 chimchar
"""

import re

# ---------------------------------------------------------------------------
# Category mapping: checklist label -> reserve category slug used in commands.
# Categories that map to None use NO category slug in the generated command
# (see Res1 / Res2 below).
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    "rares":     "rare",
    "regionals": "regional",
    "eevos":     "eevo",
    "gmax":      "gmax",
    "res1":      None,
    "res2":      None,
}

# ---------------------------------------------------------------------------
# Alias dictionary: certain Pokémon names are multi-word tokens.
# Add any additional aliases here without touching parsing logic.
# Keys are lowercase, values are the exact string sent in the command.
# ---------------------------------------------------------------------------
ALIASES = {
    "all urshifu":  "all urshifu",
    "all vivillon": "all vivillon",
    # Add more as needed, e.g.:
    # "all calyrex": "all calyrex",
}

# A compiled pattern that matches any known alias (longest first to avoid
# partial matches, e.g. "all urshifu" before "urshifu").
_ALIAS_PATTERN = re.compile(
    "|".join(
        re.escape(alias) for alias in sorted(ALIASES, key=len, reverse=True)
    ),
    re.IGNORECASE,
) if ALIASES else None


def _normalize(text: str) -> str:
    """Strip surrounding whitespace and lowercase for comparisons."""
    return text.strip().lower()


def _extract_pokemon_tokens(raw: str) -> list[str]:
    """
    Split a raw Pokémon string (e.g. "applin + all urshifu") into a list of
    individual Pokémon tokens, correctly handling multi-word aliases.

    Strategy:
    1. Replace each known alias with a placeholder so the alias text is not
       accidentally split on '+' or whitespace inside it.
    2. Split the remaining text on '+'.
    3. Restore placeholders back to alias strings.
    """
    placeholders: dict[str, str] = {}

    def _replace_alias(match: re.Match) -> str:
        key = f"__ALIAS_{len(placeholders)}__"
        placeholders[key] = ALIASES[match.group(0).lower()]
        return key

    if _ALIAS_PATTERN:
        raw = _ALIAS_PATTERN.sub(_replace_alias, raw)

    # Split on '+' and clean up each token.
    parts = [p.strip() for p in raw.split("+") if p.strip()]

    # Restore alias placeholders.
    tokens = []
    for part in parts:
        for placeholder, alias_value in placeholders.items():
            part = part.replace(placeholder, alias_value)
        tokens.append(part.strip())

    return [t for t in tokens if t]


def _parse_user_segment(segment: str) -> tuple[str, list[str]]:
    """
    Parse one user segment of the form:
        @mention  pokemon1 + pokemon2 ...
    or (when the separator between mention and Pokémon list is already split off):
        @mention

    Returns (mention_string, [pokemon_tokens]).
    """
    segment = segment.strip()

    # Find the user mention (<@id> or <@!id>).
    mention_match = re.search(r"<@!?\d+>", segment)
    if not mention_match:
        return ("", [])

    mention = mention_match.group(0)
    # Everything after the mention is the Pokémon list for this user.
    after_mention = segment[mention_match.end():].strip()

    # Remove a leading '+' separator if present.
    after_mention = after_mention.lstrip("+").strip()

    pokemon_tokens = _extract_pokemon_tokens(after_mention) if after_mention else []
    return mention, pokemon_tokens


def parse_checklist(message_content: str, reserved_set: set[str]) -> list[str]:
    """
    Parse a full checklist message and return an ordered list of command
    strings ready to send in Discord.

    Parameters
    ----------
    message_content : str
        The raw text of the checklist message that was replied to.
    reserved_set : set[str]
        A mutable set of Pokémon names (lowercase) that have already been
        reserved during this autores session.  The function updates this set
        as it processes the checklist so that later lines can detect
        conflicts with earlier ones.

    Returns
    -------
    list[str]
        Ordered list of command strings, e.g.
        ["h!r remove eternatus", "h!r add eevo, eternatus @user3"]
    """
    commands: list[str] = []
    lines = message_content.strip().splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # ------------------------------------------------------------------
        # Step 1: Detect the category prefix by splitting on the first ':'.
        # Expected format:  CategoryLabel : rest_of_line
        # ------------------------------------------------------------------
        if ":" not in line:
            continue  # Not a valid checklist line; skip silently.

        category_raw, _, remainder = line.partition(":")
        category_key = _normalize(category_raw)

        if category_key not in CATEGORY_MAP:
            continue  # Unknown category; skip silently.

        category_slug = CATEGORY_MAP[category_key]  # May be None for Res1/Res2.
        remainder = remainder.strip()

        # ------------------------------------------------------------------
        # Step 2: Handle Res2-style lines where multiple users are separated
        # by '|'.  Each sub-segment belongs to one user.
        # ------------------------------------------------------------------
        user_segments = [seg.strip() for seg in remainder.split("|")]

        for segment in user_segments:
            if not segment:
                continue

            # ----------------------------------------------------------------
            # Step 3: Parse mention and Pokémon list from this user segment.
            # The format within a segment can be:
            #   @mention + pokemon1 + pokemon2
            #   @mention pokemon1 + pokemon2   (Res1 style, space separator)
            # We always locate the mention first, then treat everything after
            # it as the Pokémon token string.
            # ----------------------------------------------------------------
            mention, pokemon_tokens = _parse_user_segment(segment)

            if not mention:
                continue  # No valid mention found; skip this segment.

            if not pokemon_tokens:
                continue  # No Pokémon listed; nothing to add.

            # ----------------------------------------------------------------
            # Step 4: Generate remove commands where needed.
            #
            # Rules:
            #   a) Any "all X" alias token always gets a remove command first.
            #   b) For Eevos (category_slug == "eevo"), if the Pokémon was
            #      already in reserved_set (reserved earlier this session),
            #      it also needs a remove command first.
            # ----------------------------------------------------------------
            for token in pokemon_tokens:
                token_lower = token.lower()
                is_alias = token_lower in ALIASES

                needs_remove = False

                if is_alias:
                    # Always remove "all X" style aliases before re-adding.
                    needs_remove = True
                elif category_slug == "eevo" and token_lower in reserved_set:
                    # Eevo Pokémon that was already reserved this session.
                    needs_remove = True

                if needs_remove:
                    commands.append(f"h!r remove {token}")

            # ----------------------------------------------------------------
            # Step 5: Generate the add command.
            #
            # Format depends on whether there is a category slug:
            #   With slug:    h!r add <slug>, pokemon1, pokemon2 @mention
            #   Without slug: h!r add pokemon1, pokemon2 @mention
            # ----------------------------------------------------------------
            pokemon_list_str = ", ".join(pokemon_tokens)

            if category_slug:
                add_cmd = f"h!r add {category_slug}, {pokemon_list_str} {mention}"
            else:
                add_cmd = f"h!r add {pokemon_list_str} {mention}"

            commands.append(add_cmd)

            # ----------------------------------------------------------------
            # Step 6: Update reserved_set so subsequent lines know what has
            # been reserved so far in this session.
            # ----------------------------------------------------------------
            for token in pokemon_tokens:
                reserved_set.add(token.lower())

    return commands
