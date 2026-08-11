"""Argus round 2 finding: cli.py had no test coverage at all."""

from reclaw_ea.cli import main


def test_main_returns_nonzero_and_explains_theres_no_cli_surface(capsys):
    exit_code = main()
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "no CLI surface yet" in captured.err
    assert captured.out == ""
