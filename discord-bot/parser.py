"""
parser.py
---------
Turns a raw checklist message into an ordered list of Discord commands.

Supported checklist formats
---------------------------

Format A — Event-time (colon separator, mention before pokemon):
    RARE #🔷| 1-rares to #🔷| 20 : @Able +cyndaquil
    EEVOS #🔷| 36-eevos to #🔷| 45 : @Able + missingno
    RES2 #🔷| 97 to #🔷| 100 : @Able poliwag | @Dusky all arceus

Format B — Non-event (arrow separator, mention may come before or after pokemon):
    >Rares + 1 ➜ (channels 1 - 20) ➜ @user +cyndaquil
    >Choice1 + 1 ➜ (channels 66 - 75) ➜ pikas + zeraora @user
    >Reserve1 x 2 ➜ (channels 86 - 90) ➜ @user bulbasaur + charmander

Parsing strategy (works for both formats)
------------------------------------------
1. Strip leading '>' and whitespace from the line.
2. The CATEGORY is always the very first word.
3. The USER+POKEMON section starts at the first Discord mention (<@id>).
   Everything before OR after the mention on that side is pokémon tokens.
   This avoids depending on which separator (: or ➜) the checklist uses.
4. Split multi-user segments on '|' ONLY within the user+pokemon section.

Special category — Choice1 / Choice2
--------------------------------------
These are NOT reserve categories themselves. The FIRST pokémon token
listed is treated as the category slug for the command.

  >Choice1 ➜ ... ➜ pikas + zeraora @user
  → h!r add pikas, zeraora @user
"""

import re

# ---------------------------------------------------------------------------
# Category mapping: first word of line (lowercase) → reserve slug or None.
#
#   slug present  →  h!r add <slug>, <pokemon> @user
#   None          →  h!r add <pokemon> @user           (no category in cmd)
#   "__choice__"  →  first pokemon token becomes the slug (see below)
# ---------------------------------------------------------------------------
CATEGORY_MAP: dict[str, str | None] = {
    # --- Reserve categories (slug sent in command) ---
    "rare":          "rare",
    "rares":         "rare",
    "regional":      "regional",
    "regionals":     "regional",
    "eevo":          "eevo",
    "eevos":         "eevo",
    "eeveelution":   "eevo",
    "eeveelutions":  "eevo",
    "gmax":          "gmax",
    "paradox":       "paradox",
    # --- Choice (first pokemon token becomes the slug) ---
    "choice1":       "__choice__",
    "choice2":       "__choice__",
    "choice3":       "__choice__",
    # --- Non-reserve categories (NO slug in command) ---
    "event1":        None,
    "event2":        None,
    "event3":        None,
    "res1":          None,
    "res2":          None,
    "res3":          None,
    "reserve1":      None,
    "reserve2":      None,
    "reserve3":      None,
}

# ---------------------------------------------------------------------------
# Alias dictionary — multi-word pokemon tokens that must not be split.
# Keys are lowercase; values are sent verbatim in the command.
# Add new aliases here without touching any other code.
# ---------------------------------------------------------------------------
ALIASES: dict[str, str] = {
    "all urshifu":  "all urshifu",
    "all vivillon": "all vivillon",
    "all arceus":   "all arceus",
    "all calyrex":  "all calyrex",
}

_ALIAS_PATTERN: re.Pattern | None = re.compile(
    "|".join(re.escape(a) for a in sorted(ALIASES, key=len, reverse=True)),
    re.IGNORECASE,
) if ALIASES else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_pokemon_tokens(raw: str) -> list[str]:
    """
    Split a raw pokemon string into individual tokens while keeping
    multi-word aliases intact.

    Splitting rules (applied after alias protection):
    - '+' present → split on '+'.
    - No '+' → split on whitespace (handles "cicada vikavolt" style).
    """
    raw = raw.strip()
    if not raw:
        return []

    placeholders: dict[str, str] = {}

    def _protect(match: re.Match) -> str:
        key = f"__ALIAS_{len(placeholders)}__"
        placeholders[key] = ALIASES[match.group(0).lower()]
        return key

    if _ALIAS_PATTERN:
        raw = _ALIAS_PATTERN.sub(_protect, raw)

    parts = (
        [p.strip() for p in raw.split("+") if p.strip()]
        if "+" in raw
        else [p.strip() for p in raw.split() if p.strip()]
    )

    tokens: list[str] = []
    for part in parts:
        for key, alias_val in placeholders.items():
            part = part.replace(key, alias_val)
        if part.strip():
            tokens.append(part.strip())

    return tokens


def _parse_user_segment(segment: str) -> tuple[str, list[str]]:
    """
    Parse one user segment into (mention, [pokemon_tokens]).

    The mention can appear BEFORE or AFTER the pokemon list:
      "@user + pikachu + zeraora"     ← mention first
      "pikas + zeraora @user"         ← mention last (Choice style)

    Pokemon on both sides of the mention are combined.
    Returns ("", []) if no valid Discord mention is found.
    """
    segment = segment.strip()
    match = re.search(r"<@!?\d+>", segment)
    if not match:
        return "", []

    mention = match.group(0)
    before = segment[: match.start()].strip().strip("+").strip()
    after  = segment[match.end() :].strip().strip("+").strip()

    # Combine pokemon text from both sides of the mention.
    combined = " + ".join(filter(None, [before, after]))
    tokens = _extract_pokemon_tokens(combined) if combined else []
    return mention, tokens


def _user_section_from_line(line: str) -> str:
    """
    Return the portion of the line that starts at the first Discord mention.
    This works regardless of whether the separator is ':' or '➜'.
    Returns "" if no mention exists on the line.
    """
    match = re.search(r"<@!?\d+>", line)
    if not match:
        return ""
    # Include any pokemon text that comes BEFORE the first mention
    # (e.g. "pikas + zeraora @user" — we want the whole segment).
    # Strategy: take from right after the last separator before the mention,
    # or from the last '➜' / ':' character before the mention position.
    pre_mention = line[: match.start()]
    # Find last hard separator before the mention
    sep_match = None
    for sep in ("➜", ":"):
        pos = pre_mention.rfind(sep)
        if pos != -1 and (sep_match is None or pos > sep_match):
            sep_match = pos

    if sep_match is not None:
        return line[sep_match + 1 :].strip()
    # No separator found before the mention — take from start of line.
    return line.strip()


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
        Raw text of the replied-to checklist message.
    reserved_set : set[str]
        Mutable set of lowercase pokemon names already reserved this session.
        Updated in-place for conflict detection on later lines.

    Returns
    -------
    list[str]
        e.g. ["h!r remove eternatus", "h!r add eevo, eternatus @user3"]
    """
    commands: list[str] = []

    for raw_line in message_content.strip().splitlines():
        # Strip Discord quote marker '>' and surrounding whitespace.
        line = raw_line.strip().lstrip(">").strip()
        if not line:
            continue

        # ------------------------------------------------------------------
        # Step 1: Category is the first word (strip trailing ':' if present).
        # ------------------------------------------------------------------
        first_word = line.split()[0].lower().rstrip(":")
        if first_word not in CATEGORY_MAP:
            continue

        category_value = CATEGORY_MAP[first_word]

        # ------------------------------------------------------------------
        # Step 2: Locate the user+pokemon section.
        # We find the last hard separator (➜ or :) BEFORE the first mention,
        # then take everything from there to end-of-line.  This handles both
        # checklist formats without caring about the separator character.
        # ------------------------------------------------------------------
        user_section = _user_section_from_line(line)
        if not user_section:
            continue

        # ------------------------------------------------------------------
        # Step 3: Split on '|' for multi-user lines (RES2 style).
        # The '|' characters in channel names appear BEFORE the first mention
        # so they are already excluded from user_section.
        # ------------------------------------------------------------------
        for segment in [s.strip() for s in user_section.split("|") if s.strip()]:

            mention, pokemon_tokens = _parse_user_segment(segment)
            if not mention or not pokemon_tokens:
                continue

            # --------------------------------------------------------------
            # Step 4: Resolve category slug.
            #
            # "__choice__" → first token is the slug, rest are pokemon.
            # str          → fixed slug.
            # None         → no slug.
            # --------------------------------------------------------------
            if category_value == "__choice__":
                # First pokemon token acts as the reserve category.
                category_slug: str | None = pokemon_tokens[0]
                pokemon_tokens = pokemon_tokens[1:]
                if not pokemon_tokens:
                    continue  # Choice line with only a category, no pokemon.
            else:
                category_slug = category_value

            # --------------------------------------------------------------
            # Step 5: Generate remove commands where needed.
            #   a) "all X" aliases always get removed first.
            #   b) Eevo pokemon already reserved this session get removed.
            # --------------------------------------------------------------
            for token in pokemon_tokens:
                token_lower = token.lower()
                needs_remove = token_lower in ALIASES or (
                    category_slug == "eevo" and token_lower in reserved_set
                )
                if needs_remove:
                    commands.append(f"h!r remove {token}")

            # --------------------------------------------------------------
            # Step 6: Build the add command.
            # --------------------------------------------------------------
            pokemon_list_str = ", ".join(pokemon_tokens)
            if category_slug:
                commands.append(f"h!r add {category_slug}, {pokemon_list_str} {mention}")
            else:
                commands.append(f"h!r add {pokemon_list_str} {mention}")

            # --------------------------------------------------------------
            # Step 7: Track reserved pokemon for conflict detection.
            # --------------------------------------------------------------
            for token in pokemon_tokens:
                reserved_set.add(token.lower())

    return commands
