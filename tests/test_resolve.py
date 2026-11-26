import pytest

from src.resolve import (
    OVERRIDES,
    build_key,
    canonical_name,
    normalize_name,
    normalize_team,
    report_unmatched,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Patrick Mahomes", "patrickmahomes"),
        ("D.J. Moore", "djmoore"),
        ("DJ Moore", "djmoore"),
        ("Amon-Ra St. Brown", "amonrastbrown"),
        ("Ja'Marr Chase", "jamarrchase"),
        ("Ja’Marr Chase", "jamarrchase"),  # curly apostrophe
        ("Kenneth Walker III", "kennethwalker"),
        ("Odell Beckham Jr.", "odellbeckham"),
        ("Michael Pittman Jr", "michaelpittman"),
        ("Marvin Harrison Jr.", "marvinharrison"),
        ("Willie Snead IV", "williesnead"),
        ("Jaxon Smith-Njigba", "jaxonsmithnjigba"),
        ("  CeeDee   LAMB ", "ceedeelamb"),
        ("Bébé Müller", "bebemuller"),  # accents stripped
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_suffix_only_stripped_at_end():
    # "ii" in the middle is not a suffix
    assert normalize_name("Ii Smith Jones") == "iismithjones"


def test_two_token_name_keeps_last_token():
    # never strip down to a single token
    assert normalize_name("Jr Smith") == "jrsmith"
    assert normalize_name("Mike V") == "mikev"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("KC", "KC"),
        ("kc", "KC"),
        ("JAC", "JAX"),
        ("LAR", "LA"),
        ("STL", "LA"),
        ("SD", "LAC"),
        ("OAK", "LV"),
        ("LVR", "LV"),
        ("WSH", "WAS"),
        ("GNB", "GB"),
        ("Kansas City Chiefs", "KC"),
        ("  new york   jets ", "NYJ"),
        ("Washington Football Team", "WAS"),
        ("49ers", "SF"),
        ("XYZ", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_team(raw, expected):
    assert normalize_team(raw) == expected


def test_overrides_are_normalized_and_canonical():
    for alias, canon in OVERRIDES.items():
        # both sides must already be in normalized form
        assert normalize_name(alias) == alias
        assert normalize_name(canon) == canon
        # no chains: a canonical value must not itself be an alias
        assert canon not in OVERRIDES


def test_canonical_name_applies_override():
    assert canonical_name("Hollywood Brown") == "marquisebrown"
    assert canonical_name("Marquise Brown") == "marquisebrown"


def test_build_key():
    assert build_key("D.J. Moore", "Chicago Bears") == "djmoore|CHI"
    assert build_key("DJ Moore", "CHI") == build_key("D.J. Moore", "chi")
    assert build_key("Hollywood Brown", "ARI") == "marquisebrown|ARI"
    assert build_key("Someone", "Nowhere") == "someone|?"


def test_report_unmatched(capsys):
    left = ["djmoore|CHI", "gabrieldavis|BUF", "ceedeelamb|DAL"]
    right = ["djmoore|CHI", "gabedavis|BUF", "ceedeelamb|DAL"]
    missing = report_unmatched(left, right, label="odds")
    assert missing == ["gabrieldavis|BUF"]
    out = capsys.readouterr().out
    assert "1 unmatched odds keys" in out


def test_report_unmatched_quiet_and_empty(capsys):
    assert report_unmatched(["a|KC"], ["a|KC"], verbose=False) == []
    assert capsys.readouterr().out == ""
