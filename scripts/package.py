#!/usr/bin/env python3
"""Zip each skill for upload to the Claude apps or any Agent Skills tool: dist/<skill>-<version>.zip.

Each zip holds one folder named after the skill, with SKILL.md at its top. The version comes from the skill's
SKILL.md metadata. Zips are reproducible (sorted entries, fixed timestamps): an unchanged skill gives the same bytes.

Run: python3 -B scripts/package.py [skill-name ...]
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "plugins" / "counterweight" / "skills"
DIST = ROOT / "dist"


def version(skill):
    front = (skill / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    return re.search(r'^\s+version: "([0-9]+\.[0-9]+\.[0-9]+)"$', front, re.M).group(1)


def files(skill):
    return sorted(p for p in skill.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def package(skill):
    DIST.mkdir(exist_ok=True)
    target = DIST / f"{skill.name}-{version(skill)}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files(skill):
            info = zipfile.ZipInfo(f"{skill.name}/{path.relative_to(skill).as_posix()}", date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if path.suffix == ".py" else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return target


def main(names):
    skills = [SKILLS / n for n in names] if names else sorted(p.parent for p in SKILLS.glob("*/SKILL.md"))
    for skill in skills:
        print(package(skill).relative_to(ROOT))


if __name__ == "__main__":
    main(sys.argv[1:])
