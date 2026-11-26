"""Player/team identity resolution.

Different sources spell the same player differently ("D.J. Moore" vs "DJ Moore",
"Kenneth Walker III" vs "Kenneth Walker") and use different team codes
("JAC" vs "JAX", "LAR" vs "LA"). Everything here turns those variants into one
canonical key so joins between nflverse stats and sportsbook lines line up.

Canonical forms:
  * player name -> lowercase ASCII, no punctuation, no suffix, no spaces
                   ("Amon-Ra St. Brown" -> "amonrastbrown")
  * team        -> nflverse team code ("JAC" -> "JAX")
  * key         -> "<name>|<TEAM>"   ("djmoore|CHI")
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable

# Generational suffixes, dropped only when they are the LAST token of a name.
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Alias name (already normalized) -> canonical name (normalized, as nflverse
# spells it in player_display_name). Add a row whenever report_unmatched()
# shows a real player that normalization alone can't fix. Verify the canonical
# side against the actual nflverse data before trusting a new entry.
OVERRIDES: dict[str, str] = {
    "hollywoodbrown": "marquisebrown",
    "gabrieldavis": "gabedavis",
    "joshpalmer": "joshuapalmer",
    "elimitchell": "elijahmitchell",
    "robbieanderson": "robbiechosen",
    "kenwalker": "kennethwalker",
    "chigokonkwo": "chigoziemokonkwo",
}

NFLVERSE_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LA", "LAC", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
}

# Alias (uppercased, whitespace-collapsed) -> nflverse code.
TEAM_ALIASES: dict[str, str] = {
    # legacy / alternate abbreviations
    "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "JAC": "JAX", "GNB": "GB", "KAN": "KC", "KCC": "KC",
    "LAR": "LA", "STL": "LA", "SD": "LAC", "SDG": "LAC",
    "OAK": "LV", "LVR": "LV", "NWE": "NE", "NEP": "NE", "NOR": "NO",
    "NOS": "NO", "SFO": "SF", "TAM": "TB", "TBB": "TB",
    "WSH": "WAS", "WFT": "WAS",
    # full names
    "ARIZONA CARDINALS": "ARI", "ATLANTA FALCONS": "ATL",
    "BALTIMORE RAVENS": "BAL", "BUFFALO BILLS": "BUF",
    "CAROLINA PANTHERS": "CAR", "CHICAGO BEARS": "CHI",
    "CINCINNATI BENGALS": "CIN", "CLEVELAND BROWNS": "CLE",
    "DALLAS COWBOYS": "DAL", "DENVER BRONCOS": "DEN",
    "DETROIT LIONS": "DET", "GREEN BAY PACKERS": "GB",
    "HOUSTON TEXANS": "HOU", "INDIANAPOLIS COLTS": "IND",
    "JACKSONVILLE JAGUARS": "JAX", "KANSAS CITY CHIEFS": "KC",
    "LOS ANGELES RAMS": "LA", "LOS ANGELES CHARGERS": "LAC",
    "LAS VEGAS RAIDERS": "LV", "MIAMI DOLPHINS": "MIA",
    "MINNESOTA VIKINGS": "MIN", "NEW ENGLAND PATRIOTS": "NE",
    "NEW ORLEANS SAINTS": "NO", "NEW YORK GIANTS": "NYG",
    "NEW YORK JETS": "NYJ", "PHILADELPHIA EAGLES": "PHI",
    "PITTSBURGH STEELERS": "PIT", "SEATTLE SEAHAWKS": "SEA",
    "SAN FRANCISCO 49ERS": "SF", "TAMPA BAY BUCCANEERS": "TB",
    "TENNESSEE TITANS": "TEN", "WASHINGTON COMMANDERS": "WAS",
    "WASHINGTON FOOTBALL TEAM": "WAS",
    # nicknames alone
    "CARDINALS": "ARI", "FALCONS": "ATL", "RAVENS": "BAL", "BILLS": "BUF",
    "PANTHERS": "CAR", "BEARS": "CHI", "BENGALS": "CIN", "BROWNS": "CLE",
    "COWBOYS": "DAL", "BRONCOS": "DEN", "LIONS": "DET", "PACKERS": "GB",
    "TEXANS": "HOU", "COLTS": "IND", "JAGUARS": "JAX", "CHIEFS": "KC",
    "RAMS": "LA", "CHARGERS": "LAC", "RAIDERS": "LV", "DOLPHINS": "MIA",
    "VIKINGS": "MIN", "PATRIOTS": "NE", "SAINTS": "NO", "GIANTS": "NYG",
    "JETS": "NYJ", "EAGLES": "PHI", "STEELERS": "PIT", "SEAHAWKS": "SEA",
    "49ERS": "SF", "NINERS": "SF", "BUCCANEERS": "TB", "BUCS": "TB",
    "TITANS": "TEN", "COMMANDERS": "WAS",
}


def normalize_name(name: str | None) -> str:
    """Collapse a player name to a join-friendly token.

    >>> normalize_name("Amon-Ra St. Brown")
    'amonrastbrown'
    >>> normalize_name("Kenneth Walker III")
    'kennethwalker'
    """
    if name is None:
        return ""
    # Split accented letters into base letter + accent mark, then drop the marks.
    decomposed = unicodedata.normalize("NFKD", str(name))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    # Punctuation between letters (D.J., Amon-Ra, Ja'Marr) joins them; turn
    # everything else non-alphanumeric into a space so tokens stay separate.
    lowered = re.sub(r"[.'’`-]", "", lowered)
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    tokens = lowered.split()
    # Drop trailing suffixes, but never reduce a name below two tokens
    # (so a first+last name that happens to end in "v" survives).
    while len(tokens) > 2 and tokens[-1] in SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def normalize_team(team: str | None) -> str | None:
    """Map any team spelling to its nflverse code. Unknown -> None."""
    if team is None:
        return None
    cleaned = " ".join(str(team).upper().split())
    if not cleaned:
        return None
    if cleaned in NFLVERSE_TEAMS:
        return cleaned
    return TEAM_ALIASES.get(cleaned)


def canonical_name(name: str | None) -> str:
    """normalize_name, then apply OVERRIDES."""
    norm = normalize_name(name)
    return OVERRIDES.get(norm, norm)


def build_key(name: str | None, team: str | None) -> str:
    """Join key: "<canonical name>|<nflverse team>"; unknown team -> "?"."""
    return f"{canonical_name(name)}|{normalize_team(team) or '?'}"


def report_unmatched(
    left: Iterable[str],
    right: Iterable[str],
    *,
    label: str = "left",
    show: int = 20,
    verbose: bool = True,
) -> list[str]:
    """Return keys in `left` that have no exact match in `right`.

    With verbose=True, also prints each miss with the closest key on the right,
    which is usually the hint for a new OVERRIDES entry.
    """
    right_set = set(right)
    missing = sorted(set(left) - right_set)
    if verbose:
        print(f"[resolve] {len(missing)} unmatched {label} keys")
        right_list = sorted(right_set)
        for key in missing[:show]:
            guess = difflib.get_close_matches(key, right_list, n=1, cutoff=0.75)
            hint = f"  (closest: {guess[0]})" if guess else ""
            print(f"  {key}{hint}")
        if len(missing) > show:
            print(f"  ... and {len(missing) - show} more")
    return missing
