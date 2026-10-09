"""Reject malformed release tags and distributions that do not match them."""

import io
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest

release = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "check_release.py"))


@pytest.mark.parametrize("tag", ["python-v0.1.0", "main", "v0.1", "v0.1.0+local", "v0.1.0;echo", "v0.1.0\n"])
def test_rejects_nonrelease_tags(tag):
    with pytest.raises(ValueError, match="release tag"):
        release["release_version"](tag)


@pytest.mark.parametrize("tag,expected", [("v0.1.0", "0.1.0"), ("v1.2.3rc1", "1.2.3rc1")])
def test_accepts_version_tags(tag, expected):
    assert release["release_version"](tag) == expected


def distributions(path, version="0.1.0", name="strands-sprites"):
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n".encode()
    with zipfile.ZipFile(path / "package.whl", "w") as archive:
        archive.writestr("package.dist-info/METADATA", metadata)
    with tarfile.open(path / "package.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("package/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


def test_checks_wheel_and_source_archive(tmp_path):
    distributions(tmp_path)
    release["check_distributions"]("v0.1.0", tmp_path)


@pytest.mark.parametrize("version,name", [("0.1.1.dev1", "strands-sprites"), ("0.1.0", "other-package")])
def test_rejects_mismatched_distribution(tmp_path, version, name):
    distributions(tmp_path, version, name)
    with pytest.raises(ValueError, match="metadata"):
        release["check_distributions"]("v0.1.0", tmp_path)


def test_rejects_missing_distributions(tmp_path):
    with pytest.raises(ValueError, match="one wheel and one source archive"):
        release["check_distributions"]("v0.1.0", tmp_path)
