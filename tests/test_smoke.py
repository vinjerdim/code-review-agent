import importlib

import pytest

from reviewer import main as cli

MODULES = ["context", "agent", "tools", "schema", "postprocess", "github_io", "main"]


@pytest.mark.parametrize("name", MODULES)
def test_modules_import(name):
    importlib.import_module(f"reviewer.{name}")


def test_cli_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "--repo" in capsys.readouterr().out


def test_cli_requires_pr_and_repo():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_cli_not_implemented_returns_nonzero():
    assert cli.main(["--pr", "1", "--repo", "o/r"]) == 1
