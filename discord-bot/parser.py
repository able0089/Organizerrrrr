"""
parser.py
---------
Turns a raw checklist message into an ordered list of Discord commands.

Supported checklist formats
---------------------------

Format A — Event-time (colon separator):
    RARE #🔷| 1-rares to #🔷| 20 : @Able +cyndaquil
    EEVOS #🔷| 36-eevos to #🔷| 45 : @Able + missingno
    RES2 #🔷| 97 to #🔷| 100 : @Able poliwag | @Dusky all arceus

Format B — Non-event (arrow separator):
    >Rares + 1 ➜ (channels 1 - 20) ➜ @user +cyndaquil
    >Choice1 + 1 ➜ (channels 66 - 75) ➜ pikas + zeraora @user
    >Reserve1 x 2 ➜ (channels 86 - 90) ➜ @user cicada vikavolt, poliwag

Pokemon splitting rules
-----------------------
Tokens are split ONLY on '+' or ','.  Spaces are NEVER used as separators.
This means "cicada vikavolt" is always treated as a single Pokémon name.

  @user cicada vikavolt          → one pokemon:  cicada vikavolt
  @user cicada vikavolt, poliwag → two pokemon:  cicada vikavolt  /  poliwag
  @user cicada vikavolt + poliwag→ two pokemon:  cicada vikavolt  /  poliwag
"""

import re

# ---------------------------------------------------------------------------
# Category mapping  →  first word of line (lowercase) : slug or sentinel
#
#   str slug       →  h!r add <slug>, <pokemon> @user
#   None           →  h!r add <pokemon> @user           (no category in cmd)
#   "__choice__"   →  first pokemon token becomes the slug
# ---------------------------------------------------------------------------
CATEGORY_MAP: dict[str, str | None] = {
    # Reserve categories (slug sent in command)
    "rare":         "rare",
    "rares":        "rare",
    "regional":     "regional",
    "regionals":    "regional",
    "eevo":         "eevos",
    "eevos":        "eevos",
    "eeveelution":  "eevos",
    "eeveelutions": "eevos",
    "gmax":         "gmax",
    "paradox":      "paradox",
    # Choice — first pokemon token becomes the slug
    "choice1":      "__choice__",
    "choice2":      "__choice__",
    "choice3":      "__choice__",
    # Non-reserve categories (no slug in command)
    "event1":       None,
    "event2":       None,
    "event3":       None,
    "res1":         None,
    "res2":         None,
    "res3":         None,
    "reserve1":     None,
    "reserve2":     None,
    "reserve3":     None,
}

# ---------------------------------------------------------------------------
# Tokens that ALWAYS need an  h!r remove  before re-adding, regardless of
# whether they appear in reserved_set.  These are category-wide holds.
# Add new "all X" entries here as needed — no other code changes required.
# ---------------------------------------------------------------------------
ALWAYS_REMOVE: set[str] = {
    # Rare — all-form pokemon
    "all arceus",
    "all articuno",
    "all deoxys",
    "all genesect",
    "all marshadow",
    "all meloetta",
    "all moltres",
    # Gmax
    "all alcremie",
    # Other multi-form aliases
    "all urshifu",
    "all vivillon",
    "all calyrex",
    "all zapdos",
    "all moltres",
    "all landorus",
    "all tornadus",
    "all thundurus",
    "all enamorus",
}

# Categories where a pokemon that was already reserved earlier in the same
# session should be removed before being re-added to a new user.
CONFLICT_CATEGORIES: set[str] = {"rare", "regional", "gmax", "eevos"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_pokemon_tokens(raw: str) -> list[str]:
    """
    Split a raw pokemon string into individual tokens.

    Splits ONLY on '+' or ',' — never on spaces.
    This preserves multi-word names like "cicada vikavolt" as a single token.
    """
    raw = raw.strip()
    if not raw:
        return []
    parts = re.split(r"[+,]", raw)
    return [p.strip() for p in parts if p.strip()]


def _parse_user_segment(segment: str) -> tuple[str, list[str]]:
    """
    Parse one user segment into (mention, [pokemon_tokens]).

    The mention can appear before OR after the pokemon list:
      "@user + pikachu, zeraora"      ← mention first
      "pikas + zeraora @user"         ← mention last (Choice style)

    Pokemon on both sides of the mention are combined.
    Returns ("", []) if no valid Discord mention is found.
    """
    segment = segment.strip()
    match = re.search(r"<@!?\d+>", segment)
    if not match:
        return "", []

    mention = match.group(0)
    before = segment[: match.start()].strip().strip("+,").strip()
    after  = segment[match.end() :].strip().strip("+,").strip()

    # Combine pokemon text from both sides of the mention using '+' so the
    # single splitter in _extract_pokemon_tokens handles it uniformly.
    combined = " + ".join(filter(None, [before, after]))
    tokens = _extract_pokemon_tokens(combined) if combined else []
    return mention, tokens


def _user_section_from_line(line: str) -> str:
    """
    Return the part of the line that starts at (or just before) the first
    Discord mention, after the last hard separator (➜ or :).

    This lets us ignore the channel/number range text that sits between the
    category name and the actual user+pokemon data, regardless of separator.
    """
    match = re.search(r"<@!?\d+>", line)
    if not match:
        return ""

    pre_mention = line[: match.start()]

    # Find the last '➜' or ':' before the mention — take everything after it.
    last_sep = -1
    for sep in ("➜", ":"):
        pos = pre_mention.rfind(sep)
        if pos > last_sep:
            last_sep = pos

    if last_sep != -1:
        return line[last_sep + 1 :].strip()

    # No separator found — take the full line from the start.
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
        Updated in-place so later lines can detect conflicts.

    Returns
    -------
    list[str]
        e.g. ["h!r remove eternatus", "h!r add eevos, eternatus @user3"]
    """
    commands: list[str] = []

    for raw_line in message_content.strip().splitlines():
        # Strip Discord quote marker '>' and surrounding whitespace.
        line = raw_line.strip().lstrip(">").strip()
        if not line:
            continue

        # Step 1 — Category: always the first word of the line.
        first_word = line.split()[0].lower().rstrip(":")
        if first_word not in CATEGORY_MAP:
            continue

        category_value = CATEGORY_MAP[first_word]

        # Step 2 — User+pokemon section: everything from (and including) the
        # text just after the last separator before the first mention.
        user_section = _user_section_from_line(line)
        if not user_section:
            continue

        # Step 3 — Split multi-user segments on '|'.
        for segment in [s.strip() for s in user_section.split("|") if s.strip()]:

            mention, pokemon_tokens = _parse_user_segment(segment)
            if not mention or not pokemon_tokens:
                continue

            # Step 4 — Resolve the category slug.
            if category_value == "__choice__":
                # First token is the reserve category; the rest are pokemon.
                category_slug: str | None = pokemon_tokens[0]
                pokemon_tokens = pokemon_tokens[1:]
                if not pokemon_tokens:
                    continue
            else:
                category_slug = category_value

            # Step 5 — Prepend remove commands where needed.
            #   a) Tokens in ALWAYS_REMOVE always get removed first.
            #   b) For rare/regional/gmax/eevos: if the pokemon was already
            #      reserved earlier this session (different user), remove it
            #      first so the new reservation doesn't conflict.
            for token in pokemon_tokens:
                token_lower = token.lower()
                needs_remove = token_lower in ALWAYS_REMOVE or (
                    category_slug in CONFLICT_CATEGORIES
                    and token_lower in reserved_set
                )
                if needs_remove:
                    commands.append(f"h!r remove {token}")

            # Step 6 — Build the add command.
            pokemon_list_str = ", ".join(pokemon_tokens)
            if category_slug:
                commands.append(f"h!r add {category_slug}, {pokemon_list_str} {mention}")
            else:
                commands.append(f"h!r add {pokemon_list_str} {mention}")

            # Step 7 — Track reserved pokemon for conflict detection.
            for token in pokemon_tokens:
                reserved_set.add(token.lower())

    return commands
