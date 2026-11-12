from jef.secrets import find_leaks

FAKE_KEY = "sk-ant-api03-" + "a" * 40


def test_find_leaks_catches_saved_secrets_and_common_shapes():
    text = "\n".join(
        [
            "fine line",
            f"KEY={FAKE_KEY}",
            "token = ghp_" + "b" * 36,
            "custom secret: hunter2hunter2",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
        ]
    )
    leaks = find_leaks(text, known_secrets=["hunter2hunter2"])
    assert [(l.line, l.kind) for l in leaks] == [
        (2, "Anthropic API key"),
        (3, "GitHub token"),
        (4, "Known secret"),
        (5, "Private key"),
    ]


def test_find_leaks_never_includes_the_secret_itself():
    leaks = find_leaks(f"x {FAKE_KEY}")
    assert FAKE_KEY not in repr(leaks)


def test_clean_text_has_no_leaks():
    assert find_leaks("def add(a, b):\n    return a + b\n") == []
