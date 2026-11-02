"""Full per-platform install + uninstall round-trip suite.

Every platform graph_fy knows about installs its SKILL.md at a specific real
destination (the ``_platform_skill_destination`` map), optionally with a
references/ sidecar, and uninstall must put the tree back exactly as it found
it. This file walks every platform through that round trip at both project and
user scope, then covers the two flows that are easy to break in the
progressive-disclosure work: a monolith -> progressive upgrade, and crash
recovery when a references staging step is interrupted.

The skill source and references bundles are the real committed package files, so
these tests exercise the actual on-disk layout an end user gets. Only Path.home
and the cwd are redirected into tmp_path so nothing touches the developer's home
directory.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import graph_fy
import graph_fy.__main__ as mainmod

pytestmark = [pytest.mark.xdist_group(name="skills_bundle")]
PKG_DIR = Path(graph_fy.__file__).resolve().parent

# Every platform in the config plus the scope each is exercised at. The
# destination is resolved from _platform_skill_destination so the assertions
# track the real install map (including amp's corrected .agents path).
ALL_CONFIG_PLATFORMS = sorted(mainmod._PLATFORM_CONFIG)


def _has_real_bundle(platform: str) -> bool:
    """True if this platform's references bundle ships in the package right now."""
    bundle = mainmod._PLATFORM_CONFIG[platform].get("skill_refs")
    if not bundle:
        return False
    return (PKG_DIR / "skills" / bundle / "references").is_dir()


@pytest.mark.parametrize("platform", ALL_CONFIG_PLATFORMS)
@pytest.mark.parametrize("project", [False, True], ids=["user", "project"])
def test_skill_roundtrip_at_real_destination(platform, project, tmp_path, monkeypatch):
    """Install then uninstall every platform's SKILL.md at its real destination.

    Install must land the skill exactly where _platform_skill_destination says,
    stamp the version, and stage references/ iff the bundle ships. Uninstall must
    remove the skill, the stamp, the references, and walk the now-empty dirs away.
    """
    home = tmp_path / "home"
    project_dir = tmp_path / "proj"
    home.mkdir()
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    with patch("graph_fy.__main__.Path.home", return_value=home):
        dst = mainmod._platform_skill_destination(
            platform, project=project, project_dir=project_dir
        )
        # Sanity: a user-scope install must not write under the project dir, and
        # vice versa, so the two scopes never collide in this test.
        if project:
            assert str(dst).startswith(str(project_dir))
        else:
            assert str(dst).startswith(str(home))

        returned = mainmod._copy_skill_file(
            platform, project=project, project_dir=project_dir
        )
        assert returned == dst
        assert dst.exists(), f"{platform} ({'project' if project else 'user'}) skill not installed"
        assert (dst.parent / ".graph_fy_version").read_text() == mainmod.__version__

        refs = dst.parent / "references"
        if _has_real_bundle(platform):
            assert refs.is_dir(), f"{platform} ships a bundle but no references/ installed"
            assert (refs / "extraction-spec.md").exists()
        else:
            assert not refs.exists(), f"{platform} is monolith but references/ appeared"
        # No staging dir is ever left behind.
        assert not (dst.parent / "references.tmp").exists()

        removed = mainmod._remove_skill_file(
            platform, project=project, project_dir=project_dir
        )
        assert removed
        assert not dst.exists()
        assert not (dst.parent / ".graph_fy_version").exists()
        assert not refs.exists()


def _install_via_entrypoint(tmp_path, platform):
    """Drive the high-level install() entry point with home + cwd in tmp_path."""
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with patch("graph_fy.__main__.Path.home", return_value=tmp_path):
            mainmod.install(platform=platform)
    finally:
        os.chdir(old_cwd)


def _copy_in_tmp(tmp_path, platform):
    """Run _copy_skill_file with home + cwd redirected into tmp_path, restoring cwd."""
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with patch("graph_fy.__main__.Path.home", return_value=tmp_path):
            mainmod._copy_skill_file(platform)
    finally:
        os.chdir(old_cwd)


def test_install_entrypoint_roundtrip(tmp_path):
    """The public install() entry point round-trips supported platforms.

    claude and copilot both install through install() and uninstall through
    _remove_skill_file, landing at and clearing their real destinations.
    """
    for platform, rel in (
        ("claude", Path(".claude") / "skills" / "graph_fy"),
        ("copilot", Path(".copilot") / "skills" / "graph_fy"),
    ):
        _install_via_entrypoint(tmp_path, platform)
        skill_dir = tmp_path / rel
        assert (skill_dir / "SKILL.md").exists()
        if _has_real_bundle(platform):
            assert (skill_dir / "references").is_dir()
        else:
            assert not (skill_dir / "references").exists()
        with patch("graph_fy.__main__.Path.home", return_value=tmp_path):
            mainmod._remove_skill_file(platform)
        assert not (skill_dir / "SKILL.md").exists()


# --- monolith -> progressive upgrade path --------------------------------------


@pytest.fixture()
def fake_progressive_bundle(tmp_path_factory, monkeypatch):
    """Stage a controllable references bundle in claude's slot.

    Lets a test flip a host between "monolith on disk" and "progressive on disk"
    deterministically without touching the real committed package on disk.
    """
    import graph_fy.install
    platform = "claude"
    bundle = mainmod._PLATFORM_CONFIG[platform]["skill_refs"]
    tmp_pkg = tmp_path_factory.mktemp("fake_bundle_root")
    bundle_dir = tmp_pkg / bundle
    refs_dir = bundle_dir / "references"

    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "extraction-spec.md").write_text("# spec\n", encoding="utf-8")
    (refs_dir / "query.md").write_text("# query\n", encoding="utf-8")

    def _mock_packaged_skill_refs_dir(p_name: str) -> Path | None:
        if p_name == platform and bundle_dir.is_dir():
            return refs_dir
        return None

    monkeypatch.setattr(graph_fy.install, "_packaged_skill_refs_dir", _mock_packaged_skill_refs_dir)
    monkeypatch.setattr(mainmod, "_packaged_skill_refs_dir", _mock_packaged_skill_refs_dir)

    yield platform, bundle_dir, refs_dir


def test_monolith_to_progressive_upgrade(tmp_path, fake_progressive_bundle):
    """A pre-progressive install (SKILL.md, no references/) gains references/ on upgrade."""
    platform, bundle_dir, _ = fake_progressive_bundle
    skill_dir = tmp_path / ".claude" / "skills" / "graph_fy"

    # Simulate the old on-disk state: a monolithic SKILL.md with no sidecar.
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old monolith body\n", encoding="utf-8")
    (skill_dir / ".graph_fy_version").write_text("0.0.1", encoding="utf-8")
    assert not (skill_dir / "references").exists()

    # Upgrade: the platform now ships a bundle, so install adds references/.
    _copy_in_tmp(tmp_path, platform)

    refs = skill_dir / "references"
    assert refs.is_dir()
    assert (refs / "extraction-spec.md").read_text() == "# spec\n"
    assert (skill_dir / ".graph_fy_version").read_text() == mainmod.__version__
    assert not (skill_dir / "references.tmp").exists()


def test_progressive_to_monolith_downgrade_clears_references(tmp_path, fake_progressive_bundle):
    """If a host loses its bundle, the next install clears the orphan references/."""
    platform, bundle_dir, _ = fake_progressive_bundle
    skill_dir = tmp_path / ".claude" / "skills" / "graph_fy"

    # First install: progressive, references/ present.
    _copy_in_tmp(tmp_path, platform)
    assert (skill_dir / "references").is_dir()

    # The bundle disappears (downgrade / build without this wave).
    shutil.rmtree(bundle_dir)
    _copy_in_tmp(tmp_path, platform)

    assert (skill_dir / "SKILL.md").exists()
    assert not (skill_dir / "references").exists(), "orphan references/ was not cleared"


# --- crash safety --------------------------------------------------------------


def test_interrupted_references_staging_self_heals(tmp_path, fake_progressive_bundle):
    """A leftover references.tmp from a crashed install is cleared on the next install."""
    platform, _, _ = fake_progressive_bundle
    skill_dir = tmp_path / ".claude" / "skills" / "graph_fy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("body\n", encoding="utf-8")

    # Simulate a crash mid-stage: a half-written references.tmp is on disk.
    staged = skill_dir / "references.tmp"
    staged.mkdir()
    (staged / "garbage.md").write_text("partial\n", encoding="utf-8")

    _copy_in_tmp(tmp_path, platform)

    refs = skill_dir / "references"
    assert refs.is_dir()
    assert (refs / "extraction-spec.md").exists()
    # The stale staging dir and its garbage are gone.
    assert not staged.exists()
    assert not (refs / "garbage.md").exists()


def test_failed_copytree_leaves_no_partial_references(tmp_path, fake_progressive_bundle):
    """If copytree blows up mid-stage, no half-written references/ is left visible."""
    platform, _, _ = fake_progressive_bundle
    skill_dir = tmp_path / ".claude" / "skills" / "graph_fy"
    skill_dir.mkdir(parents=True)
    skill_dst = skill_dir / "SKILL.md"
    skill_dst.write_text("body\n", encoding="utf-8")

    # A pre-existing good references/ that must survive a failed upgrade attempt.
    good = skill_dir / "references"
    good.mkdir()
    (good / "keep.md").write_text("keep\n", encoding="utf-8")

    boom = RuntimeError("disk full")
    with patch("graph_fy.__main__.shutil.copytree", side_effect=boom):
        with pytest.raises(RuntimeError):
            mainmod._install_skill_references(skill_dst, PKG_DIR / "skills" / "claude" / "references")

    # The staging dir is cleaned up and the existing references/ is untouched
    # (the swap only happens after a successful copytree).
    assert not (skill_dir / "references.tmp").exists()
    assert good.is_dir()
    assert (good / "keep.md").read_text() == "keep\n"
