from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_repository_contains_no_token_assignment_outside_tests() -> None:
    # 虚拟环境与 CI 检出目录里是第三方代码，不参与本仓库的凭据扫描。
    skip_parts = {".git", "tests", ".venv", ".venv-test", ".cache", "node_modules"}
    for path in REPO.rglob("*"):
        if (
            not path.is_file()
            or skip_parts & set(path.parts)
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
