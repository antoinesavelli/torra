"""
`torra.gates.options`: option parsing the way GNU getopt_long parses, and the allow-list judge.

Depends on: torra.gates.options (parse, OptionSpec, allowing), torra.gates.program_basics (READ_ONLY).
"""

from __future__ import annotations

import pytest

from torra.gates.options import OptionSpec, Parsed, allowing, parse
from torra.gates.program_basics import READ_ONLY

SPEC = OptionSpec(
    short="lnv",
    short_value="No",
    short_optional="p",
    long=frozenset({"--list", "--verbose"}),
    long_value=frozenset({"--output", "--outdir"}),
    long_optional=frozenset({"--profile"}),
)


def _parse(*words: str) -> Parsed | None:
    return parse(["prog", *words], SPEC)


def _opts(*words: str) -> tuple[tuple[str, str | None], ...] | None:
    parsed = _parse(*words)
    return None if parsed is None else parsed.options


def test_no_arguments_parses_to_nothing() -> None:
    assert _parse() == Parsed((), ())


def test_the_program_name_is_never_an_option_or_operand() -> None:
    assert parse(["-l"], SPEC) == Parsed((), ())


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (("-l",), (("-l", None),)),
        (("-ln",), (("-l", None), ("-n", None))),
        (("-l", "-n"), (("-l", None), ("-n", None))),
        (("-N50",), (("-N", "50"),)),
        (("-o/x",), (("-o", "/x"),)),
        (("-N", "50"), (("-N", "50"),)),
        (("-N", "-5"), (("-N", "-5"),)),
        (("-vN50",), (("-v", None), ("-N", "50"))),
        (("-vNl",), (("-v", None), ("-N", "l"))),
        (("-lN", "50"), (("-l", None), ("-N", "50"))),
    ],
)
def test_short_options(
    words: tuple[str, ...], expected: tuple[tuple[str, str | None], ...]
) -> None:
    assert _opts(*words) == expected


@pytest.mark.parametrize("words", [("-N",), ("-lN",), ("-x",), ("-lx",), ("-xl",), ("-l", "-N")])
def test_short_options_unknown_or_missing_value(words: tuple[str, ...]) -> None:
    assert _parse(*words) is None


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (("--list",), (("--list", None),)),
        (("--output=F",), (("--output", "F"),)),
        (("--output", "F"), (("--output", "F"),)),
        (("--output=",), (("--output", ""),)),
        (("--output=a=b",), (("--output", "a=b"),)),
    ],
)
def test_long_options(words: tuple[str, ...], expected: tuple[tuple[str, str | None], ...]) -> None:
    assert _opts(*words) == expected


@pytest.mark.parametrize(
    "words",
    [
        ("--out", "F"),
        ("--o=F",),
        ("--lis",),
        ("--li",),
        ("--outp", "F"),
        ("--outp=F",),
        ("--verb",),
        ("--prof=F",),
        ("--prof",),
        ("--=x",),
        ("--bogus",),
        ("--lists",),
        ("--list=x",),
        ("--list=",),
        ("--output",),
        ("--output=F", "--output"),
    ],
)
def test_long_options_abbreviated_unknown_or_malformed(words: tuple[str, ...]) -> None:
    assert _parse(*words) is None


def test_an_exact_name_matches_and_an_abbreviation_is_refused() -> None:
    spec = OptionSpec(long=frozenset({"--out", "--output"}))
    assert parse(["p", "--out"], spec) == Parsed((("--out", None),), ())
    assert parse(["p", "--outp"], spec) is None


def test_a_full_name_missing_from_the_spec_is_not_read_as_a_longer_spec_name() -> None:
    # curl knows --output and --output-dir; a spec holding only --output-dir must not take
    # `--output F` for it, because the real curl would write F.
    spec = OptionSpec(long_value=frozenset({"--output-dir"}))
    assert parse(["curl", "--output", "F", "u"], spec) is None
    assert parse(["curl", "--output-dir", "D", "u"], spec) == Parsed(
        (("--output-dir", "D"),), ("u",)
    )
    spec = OptionSpec(long_value=frozenset({"--log-level"}))
    assert parse(["p", "--log", "F"], spec) is None


def test_no_program_word_is_refused() -> None:
    assert parse([], SPEC) is None


@pytest.mark.parametrize(
    ("words", "options", "operands"),
    [
        (("-p",), (("-p", None),), ()),
        (("-pF",), (("-p", "F"),), ()),
        (("-p", "F"), (("-p", None),), ("F",)),
        (("-lpF",), (("-l", None), ("-p", "F")), ()),
        (("-pl",), (("-p", "l"),), ()),
        (("--profile",), (("--profile", None),), ()),
        (("--profile=F",), (("--profile", "F"),), ()),
        (("--profile", "F"), (("--profile", None),), ("F",)),
    ],
)
def test_optional_values_are_attached_only(
    words: tuple[str, ...], options: tuple[tuple[str, str | None], ...], operands: tuple[str, ...]
) -> None:
    assert _parse(*words) == Parsed(options, operands)


def test_double_dash_ends_the_options() -> None:
    parsed = _parse("-l", "--", "-n", "--list", "--")
    assert parsed == Parsed((("-l", None),), ("-n", "--list", "--"))


def test_double_dash_as_a_value_is_consumed_by_the_option() -> None:
    assert _parse("-N", "--", "x") == Parsed((("-N", "--"),), ("x",))


def test_a_lone_dash_is_an_operand() -> None:
    assert _parse("-", "-l") == Parsed((("-l", None),), ("-",))


def test_options_after_operands_still_count() -> None:
    parsed = _parse("a", "-l", "b", "--output=F", "c")
    assert parsed == Parsed((("-l", None), ("--output", "F")), ("a", "b", "c"))


def test_an_unknown_option_after_operands_is_still_refused() -> None:
    assert _parse("a", "-x") is None


def test_values_collects_every_occurrence_of_any_name() -> None:
    parsed = _parse("-o/a", "-l", "--output", "b", "--profile", "-N5")
    assert parsed is not None
    assert parsed.values("-o", "--output") == ["/a", "b"]
    assert parsed.values("--profile") == [None]
    assert parsed.values("-l") == [None]
    assert parsed.values("-v") == []
    assert parsed.values() == []


def test_allowing_accepts_a_clean_parse() -> None:
    judge = allowing(SPEC)
    assert judge(["prog", "-ln", "file"]) == READ_ONLY
    assert judge(["prog"]) == READ_ONLY


def test_allowing_refuses_what_does_not_parse() -> None:
    assert allowing(SPEC)(["prog", "--delete"]) is None


def test_allowing_runs_the_check_on_the_parse() -> None:
    judge = allowing(SPEC, lambda parsed: not parsed.values("-o", "--output"))
    assert judge(["prog", "-l", "x"]) == READ_ONLY
    assert judge(["prog", "-o/x"]) is None
    assert judge(["prog", "--output", "x"]) is None


def test_allowing_does_not_run_the_check_when_the_parse_fails() -> None:
    calls: list[Parsed] = []

    def check(parsed: Parsed) -> bool:
        calls.append(parsed)
        return True

    assert allowing(SPEC, check)(["prog", "-x"]) is None
    assert calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"short": "ln", "short_value": "n"},
        {"short": "l", "short_optional": "l"},
        {"short_value": "o", "short_optional": "o"},
        {"short": "ll"},
        {"long": frozenset({"--a"}), "long_value": frozenset({"--a"})},
        {"long_value": frozenset({"--a"}), "long_optional": frozenset({"--a"})},
        {"long": frozenset({"--a"}), "long_optional": frozenset({"--a"})},
    ],
)
def test_an_overlapping_spec_is_a_programming_error(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        OptionSpec(**kwargs)  # type: ignore[arg-type]


def test_the_same_letter_as_short_and_long_name_does_not_overlap() -> None:
    OptionSpec(short="a", long=frozenset({"--a"}))
