from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_repository_contains_no_token_assignment_outside_tests() -> None:
    for path in REPO.rglob("*"):
        if (
            not path.is_file()
            or ".git" in path.parts
            or "tests" in path.parts
        ):
            continue
        if path.suffix not in {".py", ".md", ".toml", ".yaml", ".yml"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "YOUR_ACCESS_TOKEN" not in text
        assert "Bearer YOUR" not in text


def test_gitignore_excludes_private_config() -> None:
    patterns = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "config.local.toml" in patterns
