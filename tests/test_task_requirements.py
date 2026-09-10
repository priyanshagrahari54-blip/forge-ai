from forge.agents.requirements import TaskRequirementExtractor


def test_extracts_debugging_coding_and_testing():
    extractor = TaskRequirementExtractor()

    requirements = extractor.extract(
        "Fix the authentication bug and add regression tests."
    )

    assert requirements.capabilities == (
        "debugging",
        "coding",
        "testing",
        "security",
    )


def test_extracts_security():
    extractor = TaskRequirementExtractor()

    requirements = extractor.extract(
        "Review authentication and authorization security."
    )

    assert requirements.capabilities == (
        "review",
        "security",
    )


def test_extracts_documentation():
    extractor = TaskRequirementExtractor()

    requirements = extractor.extract(
        "Update the README documentation."
    )

    assert requirements.capabilities == (
        "documentation",
    )


def test_extracts_roles_deterministically():
    extractor = TaskRequirementExtractor()

    requirements = extractor.extract(
        "Fix the bug, run tests, and review the code."
    )

    assert requirements.roles == (
        "debugging",
        "coding",
        "testing",
        "reviewing",
    )


def test_empty_task_has_no_requirements():
    extractor = TaskRequirementExtractor()

    requirements = extractor.extract("")

    assert requirements.capabilities == ()
    assert requirements.roles == ()


def test_substring_lookalikes_do_not_match():
    extractor = TaskRequirementExtractor()

    assert extractor.extract(
        "Update the latest news and contest pages.").capabilities == ()
    assert extractor.extract(
        "Show a legitimate digit on the address prefix.").capabilities == ()


def test_inflections_still_match():
    extractor = TaskRequirementExtractor()

    requirements = extractor.extract(
        "Fixing crashes in committed branches while staging deploys.")
    assert requirements.capabilities == ("debugging", "git")


def test_phrase_keyword_matches():
    extractor = TaskRequirementExtractor()

    requirements = extractor.extract("Check the repository status.")
    assert "git" in requirements.capabilities
