from __future__ import annotations

from computerlib.targets.chatgpt import compare


def test_both_languages_render_every_row_of_the_table() -> None:
    for language in compare.LANGUAGES:
        text = compare.render(language)
        for row in compare.ROWS:
            assert row.label[language] in text
            assert row.public[language] in text
            assert row.tunnel[language] in text


def test_the_table_says_public_is_the_default_that_never_changes_by_itself() -> None:
    assert "never changes by itself" in compare.render("en")
    assert "сам по себе режим не меняется" in compare.render("ru")


def test_the_tunnel_is_labelled_experimental_and_not_proven() -> None:
    en = compare.render("en")
    assert "EXPERIMENTAL" in en
    assert "not yet" in en


def test_an_unknown_language_falls_back_to_english_and_the_locale_picks_russian() -> None:
    assert compare.render("de") == compare.render("en")
    assert compare.language_from_environment("ru_RU.UTF-8") == "ru"
    assert compare.language_from_environment("en_US.UTF-8") == "en"
    assert compare.language_from_environment("") == "en"
