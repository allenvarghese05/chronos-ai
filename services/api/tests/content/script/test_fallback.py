"""Template fallback: deterministic and always valid (brief edge case 13)."""

from __future__ import annotations

import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chronos.content.script.fallback import template_script
from chronos.content.script.schema import ScriptCandidate, count_words, word_budget
from tests.content.script.helpers import DAY, candidates


def test_six_story_template_shape() -> None:
    script = template_script(candidates(6), DAY)
    assert [s.type for s in script.scenes] == ["cover"] + ["story"] * 6 + ["outro"]
    assert script.scenes[0].voiceover.startswith("Good morning. Here is your morning brief for")
    assert "Tuesday, October 6" in script.scenes[0].voiceover
    low, high = word_budget(6)
    assert low <= script.total_words <= high
    story = script.scenes[1]
    assert story.voiceover.startswith("From Source 0: Story number 0 headline.")
    assert story.kicker == "AI DROP"
    assert story.bullets == ["Summary sentence for story 0.", "A second sentence with more detail."]


def test_deterministic() -> None:
    assert template_script(candidates(4), DAY) == template_script(candidates(4), DAY)


def test_one_story_template_has_three_scenes() -> None:
    script = template_script(candidates(1), DAY)
    assert len(script.scenes) == 3


@pytest.mark.parametrize("n", [0, 8])
def test_out_of_range_counts_rejected(n: int) -> None:
    with pytest.raises(ValueError):
        template_script(candidates(n), DAY)


def test_denied_words_from_feed_are_masked() -> None:
    cand = ScriptCandidate(
        item_id=1, category="world", source_key="x", source_name="Feed",
        url="https://a.example/1", title="Minister calls report bullshit",
        summary="He said shit happens. Then left.",
    )  # fmt: skip
    script = template_script([cand], DAY)
    text = " ".join([script.scenes[1].title, script.scenes[1].voiceover, *script.scenes[1].bullets])
    assert "bullshit" not in text.casefold()
    assert "shit" not in text.casefold().split()
    assert "—" in text


def test_empty_summary_padded_with_filler() -> None:
    cand = ScriptCandidate(
        item_id=1, category="tech", source_key="x", source_name="HN",
        url="https://a.example/1", title="Short", summary=None,
    )  # fmt: skip
    script = template_script([cand], DAY)
    assert "source link saved in your feed" in script.scenes[1].voiceover
    assert script.scenes[1].bullets == []


_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=400)


@st.composite
def candidate_lists(draw: st.DrawFn) -> list[ScriptCandidate]:
    n = draw(st.integers(min_value=1, max_value=7))
    return [
        ScriptCandidate(
            item_id=i + 1,
            category=draw(st.sampled_from(["ai", "tech", "world", "india", "misc"])),
            source_key=f"s{i}",
            source_name=draw(st.text(min_size=1, max_size=80)).strip() or "Source",
            url=f"https://news.example.com/{i}",
            title=draw(st.one_of(st.just("shit fuck " * 10), _text)).strip() or "Untitled",
            summary=draw(st.one_of(st.none(), _text, st.just("x" * 300 + " " + "y" * 90))),
        )
        for i in range(n)
    ]


@settings(max_examples=300, deadline=None)
@given(candidate_lists(), st.dates(datetime.date(2020, 1, 1), datetime.date(2035, 12, 31)))
def test_template_always_validates(cands: list[ScriptCandidate], day: datetime.date) -> None:
    script = template_script(cands, day)  # validate_script runs inside; raising fails the test
    low, high = word_budget(len(cands))
    assert low <= script.total_words <= high
    assert script.total_words == sum(count_words(s.voiceover) for s in script.scenes)
