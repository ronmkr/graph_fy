"""Tests for graph_fy install --platform routing (Copilot and Claude Code)."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch
import pytest

from graph_fy.install import (
    _PLATFORM_CONFIG,
    _install_claude_hook,
    _remove_marker_section,
    install,
)


PLATFORMS = {
    "claude": (".claude/skills/graph_fy/SKILL.md",),
    "copilot": (".copilot/skills/graph_fy/SKILL.md",),
}


def _install(tmp_path, platform):
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with patch("graph_fy.__main__.Path.home", return_value=tmp_path):
            install(platform=platform)
    finally:
        os.chdir(old_cwd)


def test_install_default_claude(tmp_path):
    _install(tmp_path, "claude")
    assert (tmp_path / ".claude" / "skills" / "graph_fy" / "SKILL.md").exists()


def test_install_survives_a_winerror_17_replace(tmp_path, monkeypatch):
    """#3508: installing SKILL.md failed on some Windows setups with WinError
    17 ("cannot move to a different disk drive") from `os.replace`.
    """
    real_replace = os.replace

    def flaky_replace(src, dst):
        exc = OSError("cannot move to a different disk drive")
        exc.winerror = 17
        raise exc

    monkeypatch.setattr(os, "replace", flaky_replace)
    try:
        _install(tmp_path, "copilot")
    finally:
        monkeypatch.setattr(os, "replace", real_replace)

    skill = tmp_path / ".copilot" / "skills" / "graph_fy" / "SKILL.md"
    assert skill.exists()
    assert not any(p.name.endswith(".tmp") for p in skill.parent.iterdir())


def test_install_claude_md_honors_claude_config_dir(tmp_path, monkeypatch):
    """#2694: with CLAUDE_CONFIG_DIR set, the always-on registration lands in
    $CLAUDE_CONFIG_DIR/CLAUDE.md — not the default ~/.claude/CLAUDE.md, which the
    old code mutated regardless of the relocated profile."""
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "cfg"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        with patch("graph_fy.__main__.Path.home", return_value=home):
            install(platform="claude")
    finally:
        os.chdir(old)

    cfg_md = config / "CLAUDE.md"
    assert cfg_md.exists(), "registration did not land in $CLAUDE_CONFIG_DIR"
    text = cfg_md.read_text()
    assert "# graph_fy" in text
    assert str(config) in text, "skill reference does not point into the config dir"
    assert not (home / ".claude" / "CLAUDE.md").exists(), "default profile was mutated"


def test_install_claude_md_defaults_to_home_when_config_dir_unset(tmp_path, monkeypatch):
    """Env unset: behavior is unchanged — the block lands in ~/.claude/CLAUDE.md
    with the tilde skill reference."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        with patch("graph_fy.__main__.Path.home", return_value=tmp_path):
            install(platform="claude")
    finally:
        os.chdir(old)

    md = tmp_path / ".claude" / "CLAUDE.md"
    assert md.exists()
    assert "~/.claude/skills/graph_fy/SKILL.md" in md.read_text()


def _deny_writes_to(target: Path, monkeypatch):
    """Make write_text raise PermissionError for *target* only (simulates a
    dotfile symlinked into a read-only store, e.g. /nix/store)."""
    real_write_text = Path.write_text

    def guarded(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded)


def test_install_survives_unwritable_claude_md(tmp_path, monkeypatch, capsys):
    """#3474: a read-only ~/.claude/CLAUDE.md must not abort the install."""
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".claude" / "CLAUDE.md"
    target.parent.mkdir(parents=True)
    target.write_text("# my rules\n")

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    with patch("graph_fy.__main__.Path.home", return_value=home):
        _deny_writes_to(target, monkeypatch)
        install(platform="claude")  # must not raise

    assert (home / ".claude" / "skills" / "graph_fy" / "SKILL.md").exists(), (
        "skill files should still be installed"
    )
    assert target.read_text() == "# my rules\n", "unwritable file must be untouched"
    err = capsys.readouterr().err
    assert "skipped" in err
    assert "PermissionError" in err


def test_install_claude_md_success_output_unchanged(tmp_path, monkeypatch, capsys):
    """Happy path output is untouched: 'created at' on first install,
    'already registered' on idempotent re-install."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    with patch("graph_fy.__main__.Path.home", return_value=home):
        install(platform="claude")
        first = capsys.readouterr().out
        install(platform="claude")
        second = capsys.readouterr().out

    assert "  CLAUDE.md        ->  created at " in first
    assert "  CLAUDE.md        ->  already registered (no change)" in second


def test_install_project_claude_writes_project_scope(tmp_path, monkeypatch, capsys):
    from graph_fy.__main__ import main
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["graph_fy", "install", "--project"])
    with patch("graph_fy.__main__.Path.home", return_value=home):
        main()
    assert (project / ".claude" / "skills" / "graph_fy" / "SKILL.md").exists()
    assert (project / ".claude" / "CLAUDE.md").exists()
    assert not (home / ".claude" / "skills" / "graph_fy" / "SKILL.md").exists()
    assert ".claude/skills/graph_fy/SKILL.md" in (project / ".claude" / "CLAUDE.md").read_text()
    assert "~/.claude/skills/graph_fy/SKILL.md" not in (project / ".claude" / "CLAUDE.md").read_text()
    assert "git add .claude/" in capsys.readouterr().out


def test_claude_subcommand_project_install_and_uninstall_are_project_scoped(tmp_path, monkeypatch):
    from graph_fy.__main__ import main
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    user_skill = home / ".claude" / "skills" / "graph_fy" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("user skill")
    monkeypatch.chdir(project)
    with patch("graph_fy.__main__.Path.home", return_value=home):
        monkeypatch.setattr(sys, "argv", ["graph_fy", "claude", "install", "--project"])
        main()
        assert (project / ".claude" / "skills" / "graph_fy" / "SKILL.md").exists()
        assert (project / ".claude" / "CLAUDE.md").exists()
        assert (project / "CLAUDE.md").exists()
        assert user_skill.exists()

        monkeypatch.setattr(sys, "argv", ["graph_fy", "claude", "uninstall", "--project"])
        main()

    assert user_skill.exists()
    assert not (project / ".claude" / "skills" / "graph_fy" / "SKILL.md").exists()
    assert not (project / ".claude" / "CLAUDE.md").exists()
    assert not (project / "CLAUDE.md").exists()


def test_copilot_install_and_uninstall(tmp_path, monkeypatch):
    from graph_fy.__main__ import main
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    with patch("graph_fy.__main__.Path.home", return_value=home):
        monkeypatch.setattr(sys, "argv", ["graph_fy", "copilot", "install"])
        main()
        skill = home / ".copilot" / "skills" / "graph_fy" / "SKILL.md"
        assert skill.exists()

        monkeypatch.setattr(sys, "argv", ["graph_fy", "copilot", "uninstall"])
        main()
        assert not skill.exists()


def test_copilot_project_install_and_uninstall(tmp_path, monkeypatch):
    from graph_fy.__main__ import main
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    with patch("graph_fy.__main__.Path.home", return_value=home):
        monkeypatch.setattr(sys, "argv", ["graph_fy", "install", "--project", "--platform", "copilot"])
        main()
        skill = project / ".copilot" / "skills" / "graph_fy" / "SKILL.md"
        assert skill.exists()

        monkeypatch.setattr(sys, "argv", ["graph_fy", "uninstall", "--project", "--platform", "copilot"])
        main()
        assert not skill.exists()


def test_install_help_does_not_install_default(tmp_path, monkeypatch, capsys):
    from graph_fy.__main__ import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["graph_fy", "install", "--help"])
    with patch("graph_fy.__main__.Path.home", return_value=tmp_path):
        main()
    out = capsys.readouterr().out
    assert "Usage: graph_fy install" in out or "Usage: graph_fy install" in out
    assert "claude" in out
    assert "copilot" in out
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".copilot").exists()


@pytest.mark.parametrize("bad_platform", ["unknown", "cursor", "codex", "opencode", "kilo", "hermes", "vscode"])
def test_install_unknown_platform_exits(tmp_path, bad_platform):
    with pytest.raises(SystemExit) as exc_info:
        _install(tmp_path, bad_platform)
    assert exc_info.value.code == 1


def test_all_skill_files_exist_in_package():
    """All installable platform skill files must be present in the installed package."""
    import graph_fy

    pkg = Path(graph_fy.__file__).parent
    for name in (
        "skill.md",
        "skill-copilot.md",
    ):
        assert (pkg / name).exists(), f"Missing: {name}"


def test_claude_install_registers_claude_md(tmp_path):
    """Claude platform install writes CLAUDE.md."""
    _install(tmp_path, "claude")
    assert (tmp_path / ".claude" / "CLAUDE.md").exists()


def test_claude_hook_is_shell_agnostic(tmp_path):
    # #522: the installed PreToolUse hooks must be plain exe invocations, not
    # POSIX bash (which fails on Windows cmd.exe/PowerShell).
    import json as _json
    _install_claude_hook(tmp_path)
    hooks = _json.loads((tmp_path / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    matchers = {h["matcher"] for h in hooks}
    assert {"Bash|Grep", "Read|Glob"} <= matchers
    for h in hooks:
        cmd = h["hooks"][0]["command"]
        for token in ("$(", "case ", "[ -f", "&&", "||", ";;", "echo '"):
            assert token not in cmd, f"shell syntax {token!r} in {cmd!r}"
        assert "graph_fy" in cmd and "hook-guard" in cmd


def test_claude_hook_install_idempotent_and_replaces_old_bash_hook(tmp_path):
    import json as _json
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    # Pre-seed a legacy bash-style graph_fy hook (the thing #522 shipped before).
    settings_path.write_text(_json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command",
         "command": "[ -f graph_fy_out/graph.json ] && echo '{...}' || true"}]},
    ]}}), encoding="utf-8")
    _install_claude_hook(tmp_path)
    _install_claude_hook(tmp_path)  # second install must not duplicate
    hooks = _json.loads(settings_path.read_text())["hooks"]["PreToolUse"]
    graph_fy_hooks = [h for h in hooks if "graph_fy" in str(h)]
    assert len(graph_fy_hooks) == 2, "exactly the Bash + Read|Glob guards, no dupes"
    # the legacy bash payload must be gone
    assert not any("[ -f graph_fy_out" in h["hooks"][0]["command"] for h in graph_fy_hooks)


def test_uninstall_project_removes_project_skill_only(tmp_path, monkeypatch):
    from graph_fy.__main__ import main
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    user_skill = home / ".copilot" / "skills" / "graph_fy" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("user skill")
    monkeypatch.chdir(project)
    with patch("graph_fy.__main__.Path.home", return_value=home):
        monkeypatch.setattr(sys, "argv", ["graph_fy", "install", "--project", "--platform", "copilot"])
        main()
        monkeypatch.setattr(sys, "argv", ["graph_fy", "uninstall", "--project", "--platform", "copilot"])
        main()
    assert user_skill.exists()
    assert not (project / ".copilot" / "skills" / "graph_fy" / "SKILL.md").exists()


def test_uninstall_project_without_platform_removes_project_installs(tmp_path, monkeypatch):
    from graph_fy.__main__ import main
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    user_skill = home / ".claude" / "skills" / "graph_fy" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("user skill")
    monkeypatch.chdir(project)
    with patch("graph_fy.__main__.Path.home", return_value=home):
        monkeypatch.setattr(sys, "argv", ["graph_fy", "install", "--project"])
        main()
        monkeypatch.setattr(sys, "argv", ["graph_fy", "uninstall", "--project"])
        main()
    assert user_skill.exists()
    assert not (project / ".claude" / "skills" / "graph_fy" / "SKILL.md").exists()
    assert not (project / ".claude" / "CLAUDE.md").exists()


def test_remove_marker_section_matches_exact_heading_only():
    """#2062: the strip helper must match graph_fy's own `## graph_fy` heading
    exactly, never a substring inside a user's `### graph_fy` H3."""
    m = "## graph_fy"

    # Only a user H3 mention -> no exact marker line -> None (file left untouched).
    assert _remove_marker_section("# Doc\n\n### graph_fy\n\nmy notes\n", m) is None
    # An inline/bullet mention is likewise not a section.
    assert _remove_marker_section("see the ## graph_fy bullet\n", m) is None

    # A real H2 section alongside a user H3: remove only the H2 section.
    content = "# Doc\n\n### graph_fy\n\nmy notes\n\n## graph_fy\n\ngraph_fy stuff\n"
    out = _remove_marker_section(content, m)
    assert out is not None
    assert "### graph_fy" in out and "my notes" in out
    assert not any(l.strip() == "## graph_fy" for l in out.splitlines())
    assert "graph_fy stuff" not in out

    # The section runs to the next H2 (not stopping at a `###` inside it).
    c2 = "## graph_fy\n\nintro\n\n### sub\n\ninner\n\n## Keep\n\nkeep me\n"
    out2 = _remove_marker_section(c2, m)
    assert "## Keep" in out2 and "keep me" in out2
    assert "inner" not in out2 and "intro" not in out2


_PROJECT_HOOK_FILES = {
    "claude": ".claude/settings.json",
}


def _hook_commands(text: str) -> list:
    """Every hook command string in a settings/hooks JSON document."""
    import json as _json

    doc = _json.loads(text)
    found = []
    for groups in doc.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                if "command" in hook:
                    found.append(hook["command"])
    return found


def _run_project_install(project, home, platform):
    from graph_fy.__main__ import main

    with patch("graph_fy.__main__.Path.home", return_value=home):
        with patch("sys.argv", ["graph_fy", "install", "--project", "--platform", platform]):
            main()


@pytest.mark.parametrize("platform", sorted(_PROJECT_HOOK_FILES))
def test_project_install_hook_command_is_portable(tmp_path, monkeypatch, platform):
    """A committed hook must carry no absolute path, drive letter or .EXE casing."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("shutil.which", lambda _name: r"C:\Users\installer\graph_fy.EXE")

    _run_project_install(project, home, platform)

    commands = _hook_commands((project / _PROJECT_HOOK_FILES[platform]).read_text(encoding="utf-8"))
    assert commands, f"{platform} project install registered no hook command"
    for command in commands:
        assert command.startswith("graph_fy ") or command.startswith("graph_fy "), command
        assert ":" not in command, f"drive letter / absolute path leaked: {command}"
        assert "\\" not in command, f"backslash path leaked: {command}"
        assert ".exe" not in command.lower(), f"platform exe casing leaked: {command}"
        assert "installer" not in command, f"installing user's path leaked: {command}"


@pytest.mark.parametrize("platform", sorted(_PROJECT_HOOK_FILES))
def test_user_profile_install_still_resolves_absolute_path(tmp_path, monkeypatch, platform):
    """The non-project install keeps the resolved path (#522/#1987 behaviour)."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("shutil.which", lambda _name: r"C:\Users\installer\graph_fy.EXE")

    from graph_fy.__main__ import main

    with patch("graph_fy.__main__.Path.home", return_value=home):
        with patch("sys.argv", ["graph_fy", platform, "install"]):
            main()

    commands = _hook_commands((project / _PROJECT_HOOK_FILES[platform]).read_text(encoding="utf-8"))
    assert commands, f"{platform} install registered no hook command"
    for command in commands:
        assert command.startswith("C:/Users/installer/graph_fy.EXE "), command


@pytest.mark.parametrize("platform", sorted(_PROJECT_HOOK_FILES))
def test_project_install_is_idempotent(tmp_path, monkeypatch, platform):
    """Installing twice leaves byte-identical config (no churn on re-install)."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("shutil.which", lambda _name: r"C:\Users\installer\graph_fy.EXE")
    target = project / _PROJECT_HOOK_FILES[platform]

    _run_project_install(project, home, platform)
    first = target.read_text(encoding="utf-8")
    _run_project_install(project, home, platform)

    assert target.read_text(encoding="utf-8") == first


def test_project_uninstall_removes_the_bare_hook_command(tmp_path, monkeypatch):
    """The uninstall filter matches on "graph_fy", so a bare command still goes."""
    from graph_fy.__main__ import main

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("shutil.which", lambda _name: r"C:\Users\installer\graph_fy.EXE")

    _run_project_install(project, home, "claude")
    settings = project / ".claude" / "settings.json"
    assert any("hook-guard" in c for c in _hook_commands(settings.read_text(encoding="utf-8")))

    with patch("graph_fy.__main__.Path.home", return_value=home):
        with patch("sys.argv", ["graph_fy", "claude", "uninstall", "--project"]):
            main()

    assert not [c for c in _hook_commands(settings.read_text(encoding="utf-8")) if "graph_fy" in c]


def test_copilot_install_with_dangling_symlink_parent(tmp_path, requires_symlinks):
    """Ensure install handles dangling symlinks in parent directories (e.g. ~/.copilot -> ~/.config/copilot)."""
    home = tmp_path / "home"
    home.mkdir()
    target_config = home / ".config" / "copilot"
    copilot_symlink = home / ".copilot"
    copilot_symlink.symlink_to(target_config)
    assert not target_config.exists()
    assert copilot_symlink.is_symlink()
    assert not copilot_symlink.exists()

    with patch("graph_fy.__main__.Path.home", return_value=home):
        install(platform="copilot")

    skill = home / ".copilot" / "skills" / "graph_fy" / "SKILL.md"
    assert skill.exists()
    assert target_config.exists()


def test_install_agent_alias_resolves_to_copilot(tmp_path):
    """'agent' alias installs to copilot destination."""
    from graph_fy.__main__ import main

    home = tmp_path / "home"
    home.mkdir()
    with patch("graph_fy.__main__.Path.home", return_value=home):
        with patch("sys.argv", ["graph_fy", "install", "agent"]):
            main()

    assert (home / ".copilot" / "skills" / "graph_fy" / "SKILL.md").exists()


def test_agent_subcommand_install_and_uninstall(tmp_path):
    """'graph_fy agent install' and uninstall route to copilot."""
    from graph_fy.__main__ import main

    home = tmp_path / "home"
    home.mkdir()
    with patch("graph_fy.__main__.Path.home", return_value=home):
        with patch("sys.argv", ["graph_fy", "agent", "install"]):
            main()
        skill = home / ".copilot" / "skills" / "graph_fy" / "SKILL.md"
        assert skill.exists()

        with patch("sys.argv", ["graph_fy", "agent", "uninstall"]):
            main()
        assert not skill.exists()


def test_claude_subcommand_installs_skill(tmp_path, monkeypatch):
    """'graph_fy claude install' installs the skill as well as registering hooks/md."""
    from graph_fy.__main__ import main

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.chdir(project)
    with patch("graph_fy.__main__.Path.home", return_value=home), \
         patch("sys.argv", ["graph_fy", "claude", "install"]):
        main()

    assert (home / ".claude" / "skills" / "graph_fy" / "SKILL.md").exists()
    assert (project / "CLAUDE.md").exists()


def test_gemini_and_antigravity_subcommand_install_and_uninstall(tmp_path):
    """'graph_fy gemini install' and 'graph_fy antigravity install' route properly."""
    from graph_fy.__main__ import main

    home = tmp_path / "home"
    home.mkdir()
    with patch("graph_fy.__main__.Path.home", return_value=home):
        with patch("sys.argv", ["graph_fy", "gemini", "install"]):
            main()
        skill = home / ".gemini" / "config" / "skills" / "graph_fy" / "SKILL.md"
        assert skill.exists()

        with patch("sys.argv", ["graph_fy", "gemini", "uninstall"]):
            main()
        assert not skill.exists()

        with patch("sys.argv", ["graph_fy", "antigravity", "install"]):
            main()
        assert skill.exists()

        with patch("sys.argv", ["graph_fy", "antigravity", "uninstall"]):
            main()
        assert not skill.exists()


def test_uninstall_all_removes_skills_and_invokes_binary_uninstall(tmp_path, monkeypatch):
    """'graph_fy uninstall' removes skills across platforms and attempts binary uninstall."""
    from graph_fy.__main__ import main

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    for p in ("copilot", "gemini"):
        with patch("graph_fy.__main__.Path.home", return_value=home):
            with patch("sys.argv", ["graph_fy", p, "install"]):
                main()

    assert (home / ".copilot" / "skills" / "graph_fy" / "SKILL.md").exists()
    assert (home / ".gemini" / "config" / "skills" / "graph_fy" / "SKILL.md").exists()

    with patch("graph_fy.__main__.Path.home", return_value=home), \
         patch("graph_fy.install._uninstall_binary", return_value=True) as mock_bin_uninst:
        with patch("sys.argv", ["graph_fy", "uninstall"]):
            main()

    assert not (home / ".copilot" / "skills" / "graph_fy" / "SKILL.md").exists()
    assert not (home / ".gemini" / "config" / "skills" / "graph_fy" / "SKILL.md").exists()
    assert mock_bin_uninst.called


