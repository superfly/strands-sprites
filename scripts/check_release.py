"""Check the release tag and distribution metadata before publishing."""

import argparse
import re
import tarfile
import zipfile
from email import message_from_bytes
from pathlib import Path


def release_version(tag: str) -> str:
    """Accept plain v-prefixed release versions, including PEP 440 prereleases."""
    match = re.fullmatch(r"v([0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?)", tag)
    if match is None:
        raise ValueError("Expected a release tag such as v0.1.0 or v0.1.0rc1")
    return match.group(1)


def check_distributions(tag: str, directory: Path) -> None:
    """Require one wheel and one source archive with matching name and version."""
    version = release_version(tag)
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1 or len(list(directory.iterdir())) != 2:
        raise ValueError("Expected exactly one wheel and one source archive")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("Invalid wheel metadata")
        metadata = [message_from_bytes(wheel.read(names[0]))]
    with tarfile.open(sources[0], "r:gz") as source:
        names = [name for name in source.getnames() if name.endswith("/PKG-INFO") and name.count("/") == 1]
        if len(names) != 1:
            raise ValueError("Invalid source archive metadata")
        info = source.extractfile(names[0])
        if info is None:
            raise ValueError("Missing source archive metadata")
        with info:
            metadata.append(message_from_bytes(info.read()))
    for item in metadata:
        if item["Name"] != "strands-sprites" or item["Version"] != version:
            raise ValueError(f"Distribution metadata must identify strands-sprites {version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--tag-only", action="store_true")
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.tag_only:
        release_version(args.tag)
    else:
        check_distributions(args.tag, args.directory)


if __name__ == "__main__":
    main()
