"""Checks for every skill in the plugin, plus an end-to-end run of each skill's bundled demo.

Run: python3 -B -m unittest discover -s tests -v
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "counterweight"
SKILLS = sorted(p.parent for p in (PLUGIN / "skills").glob("*/SKILL.md"))
sys.path.insert(0, str(ROOT / "scripts"))
import package  # noqa: E402

# The Agent Skills specification (agentskills.io) and the fields the Claude apps accept on upload.
ALLOWED_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def frontmatter(skill):
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    fields, current = {}, None
    for line in match.group(1).splitlines():
        if line.startswith((" ", "\t")):
            fields[current] += "\n" + line.strip()
        else:
            current, _, value = line.partition(":")
            fields[current] = value.strip()
    return fields


class SkillTest(unittest.TestCase):
    def test_there_are_skills(self):
        self.assertTrue(SKILLS)

    def test_frontmatter_follows_the_spec(self):
        for skill in SKILLS:
            with self.subTest(skill=skill.name):
                fields = frontmatter(skill)
                self.assertLessEqual(set(fields), ALLOWED_FIELDS, "the Claude apps reject other frontmatter keys")
                self.assertEqual(fields["name"], skill.name, "name must match the folder")
                self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertLessEqual(len(fields["name"]), 64)
                self.assertTrue(0 < len(fields["description"]) <= 1024)
                self.assertRegex(fields.get("metadata", ""), r'version: "\d+\.\d+\.\d+"')

    def test_skill_md_stays_short(self):
        for skill in SKILLS:
            with self.subTest(skill=skill.name):
                self.assertLess(len((skill / "SKILL.md").read_text(encoding="utf-8").splitlines()), 500)

    def test_manifests_agree(self):
        marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        entry = next(p for p in marketplace["plugins"] if p["name"] == plugin["name"])
        self.assertEqual(entry["source"], "./plugins/counterweight")
        self.assertEqual(plugin["license"], "Apache-2.0")

    def test_package_is_one_folder_with_skill_md_on_top(self):
        for skill in SKILLS:
            with self.subTest(skill=skill.name), tempfile.TemporaryDirectory() as out:
                package.DIST = Path(out)
                path = package.package(skill)
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                self.assertIn(f"{skill.name}/SKILL.md", names)
                self.assertTrue(all(n.startswith(skill.name + "/") for n in names))
                self.assertFalse(any("__pycache__" in n for n in names))
                self.assertEqual(path.read_bytes(), package.package(skill).read_bytes(), "zips must be reproducible")


class BareMetalEstimateDemo(unittest.TestCase):
    """counterweight-bare-metal-estimate on its bundled synthetic bill, as SKILL.md's demo describes."""

    SKILL = PLUGIN / "skills" / "counterweight-bare-metal-estimate"

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        scripts, work = cls.SKILL / "scripts", Path(cls.work.name)
        steps = [
            ["ingest.py", str(cls.SKILL / "assets" / "sample-aws-cur.csv"), "-o", "estate.json"],
            ["estimate.py", "estate.json", "-o", "plan.json"],
            ["report.py", "plan.json", "-o", "report.md", "--html", "report.html", "--client", "Sample Co"],
        ]
        for script, *args in steps:
            subprocess.run([sys.executable, "-B", str(scripts / script), *args], cwd=work, check=True,
                           capture_output=True)
        cls.plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
        cls.html = (work / "report.html").read_text(encoding="utf-8")
        cls.md = (work / "report.md").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_plan_has_both_scenarios_for_the_default_providers(self):
        for provider in ("hetzner", "ovhcloud"):
            with self.subTest(provider=provider):
                scenarios = self.plan["providers"][provider]["scenarios"]
                self.assertEqual(set(scenarios), {"like_for_like", "right_sized"})

    def test_report_makes_no_requests_to_other_sites(self):
        urls = set(re.findall(r'(?:src|href)="(https?://[^"]+)"', self.html))
        urls |= set(re.findall(r"url\((https?://[^)]+)\)", self.html))
        self.assertEqual(urls, set())

    def test_report_embeds_its_fonts(self):
        for family in ("Archivo", "IBM Plex Mono"):
            with self.subTest(family=family):
                self.assertIn(f'font-family:"{family}";src:url(data:font/woff2;base64,', self.html)

    def test_report_names_the_company(self):
        for report in (self.html, self.md):
            self.assertIn("Unicorn Operations Limited, registered in Ireland, company number 774973", report)


if __name__ == "__main__":
    unittest.main()
