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

    def test_release_please_versions_every_skill(self):
        plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        config = json.loads((ROOT / "release-please-config.json").read_text(encoding="utf-8"))
        extra = {f["path"] for f in config["packages"]["."]["extra-files"]}
        self.assertIn("plugins/counterweight/.claude-plugin/plugin.json", extra)
        for skill in SKILLS:
            with self.subTest(skill=skill.name):
                self.assertIn((skill / "SKILL.md").relative_to(ROOT).as_posix(), extra,
                              "add the skill's SKILL.md to extra-files in release-please-config.json")
                self.assertRegex((skill / "SKILL.md").read_text(encoding="utf-8"),
                                 r'(?m)^  version: "' + re.escape(plugin["version"]) + r'" # x-release-please-version$',
                                 "metadata.version must match plugin.json and carry release-please's marker")

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
        for provider in ("hetzner", "ovhcloud", "scaleway"):
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


class BareMetalEstimateInputs(unittest.TestCase):
    """Each supported bill format, from fixtures built on the providers' documented column names."""

    SKILL = PLUGIN / "skills" / "counterweight-bare-metal-estimate"
    FIXTURES = ROOT / "tests" / "fixtures"

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)

    def run_script(self, script, *args):
        return subprocess.run([sys.executable, "-B", str(self.SKILL / "scripts" / script), *args], cwd=self.work.name,
                              check=True, capture_output=True, text=True)

    def ingest(self, *fixtures, extra=()):
        self.run_script("ingest.py", *[str(self.FIXTURES / f) for f in fixtures], "-o", "estate.json", *extra)
        return json.loads((Path(self.work.name) / "estate.json").read_text(encoding="utf-8"))

    def estimate(self, *args):
        self.run_script("estimate.py", "estate.json", "-o", "plan.json", *args)
        return json.loads((Path(self.work.name) / "plan.json").read_text(encoding="utf-8"))

    @staticmethod
    def by_category(estate):
        out = {}
        for item in estate["items"]:
            out.setdefault(item["category"], []).append(item)
        return out

    def test_oci_cost_report(self):
        estate = self.ingest("oci-cost-report.csv")
        meta, cats = estate["meta"], self.by_category(estate)
        self.assertEqual(meta["input_kind"], ["oci"])
        self.assertEqual(meta["currency"], "USD")
        self.assertAlmostEqual(meta["months_covered"], 720 / 730, places=3)
        self.assertAlmostEqual(meta["total_cost_ex_tax_ex_credits"], 9058.66, places=2)
        self.assertEqual(meta["unclassified_top"], [])
        compute = {i["series"]: i for i in cats["compute"] if i.get("series")}
        # x86: one OCPU is two vCPUs; Ampere A1: one OCPU is one core (no SMT)
        self.assertEqual(compute["oci-e5"]["vcpu_hours"], 2 * 16 * 720 * 2)
        self.assertEqual(compute["oci-e5"]["ram_gb_hours"], 2 * 256 * 720)
        self.assertEqual(compute["oci-a1"]["vcpu_hours"], 4 * 8 * 720)
        self.assertEqual(compute["oci-e4"]["vcpu_hours"], (6 * 4 * 720 - 4) * 2, "the correction row nets off")
        db = cats["database"][0]
        self.assertEqual((db["engine"], db["vcpu_hours"]), ("postgres", 2 * 4 * 720 * 2),
                         "the PostgreSQL service fee is a fee, not more vCPUs")
        self.assertEqual(sum(i.get("storage_gb_month", 0) for i in cats["block_storage"]), 13200,
                         "performance units are not storage")
        self.assertEqual(sum(i.get("transfer_gb", 0) for i in cats["egress"]), 60000)
        self.assertEqual(cats["archive_storage"][0]["storage_gb_month"], 100000)
        self.assertEqual(cats["gpu"][0]["gpu_model"], "A10")
        self.assertEqual(cats["kubernetes_control"][0]["usage_hours"], 720)

    def test_oci_focus_report_matches_the_cost_report(self):
        focus = self.ingest("oci-focus.csv")
        self.assertEqual(focus["meta"]["input_kind"], ["focus"])
        self.assertEqual({i["cloud"] for i in focus["items"]}, {"oci"})
        cats = self.by_category(focus)
        self.assertEqual(sum(i.get("vcpu_hours", 0) for i in cats["compute"]), (6 * 4 + 2 * 16) * 720 * 2 + 4 * 8 * 720)
        self.assertEqual(sum(i.get("transfer_gb", 0) for i in cats["egress"]), 60000)
        plan = self.estimate()
        self.assertEqual(plan["input"]["input_kind"], ["focus"])
        self.assertGreater(plan["confidence"]["share_of_sized_spend_from_usage_data"], 0.8)

    def test_oci_estate_is_sized_and_says_when_bare_metal_does_not_pay(self):
        self.ingest("oci-cost-report.csv")
        plan = self.estimate()
        arm = [f for f in plan["flags"] if "Arm" in f["text"]]
        self.assertTrue(arm, "Ampere A1 capacity is reported as Arm")
        self.run_script("report.py", "plan.json", "-o", "report.md")
        md = (Path(self.work.name) / "report.md").read_text(encoding="utf-8")
        self.assertIn("an Oracle Cloud cost report", md)
        rec = plan["providers"][plan["recommended_provider"]]["scenarios"]["right_sized"]
        if rec["saving_eur_month"] <= 0:
            self.assertIn("No saving at this size", md)

    def test_ibm_instances_csv(self):
        """Headers verbatim from IBM's Instances CSV v1.4.1 docs; the section-title rows above them are assumed."""
        estate = self.ingest("ibm-instances.csv")
        meta, cats = estate["meta"], self.by_category(estate)
        self.assertEqual(meta["input_kind"], ["ibm"])
        self.assertEqual((meta["period_start"], meta["period_hours"]), ("2026-09-01", 720))
        self.assertAlmostEqual(meta["total_cost_ex_tax_ex_credits"], 13202.53, places=2, msg="non-chargeable rows left out")
        self.assertEqual(meta["unclassified_top"], [])
        vsi = {i.get("instance_type"): i for i in cats["compute"]}
        self.assertEqual(vsi["bx3d-16x80"]["vcpu_hours"], 6 * 720 * 16, "profile decoded")
        self.assertEqual(vsi["bx3d-16x80"]["ram_gb_hours"], 6 * 720 * 80)
        iks = next(i for i in cats["compute"] if i["service"] == "Kubernetes Service")
        self.assertEqual(iks["vcpu_hours"], 3 * 720 * 16, "worker flavour read from the plan name")
        classic = next(i for i in cats["compute"] if i["service"].startswith("Virtual Servers and"))
        self.assertEqual((classic["vcpu_hours"], classic["ram_gb_hours"]), (4 * 720, 8 * 720),
                         "classic CHARGE rows: cores and RAM from the metric, not the money quantity")
        db = cats["database"][0]
        self.assertEqual((db["engine"], db["vcpu_hours"], db["ram_gb_hours"]), ("postgres", 8640, 48 * 720))
        self.assertTrue(db["replicas_included"])
        self.assertEqual(cats["archive_storage"][0]["storage_gb_month"], 100000)
        self.assertEqual(cats["object_storage"][0]["storage_gb_month"], 50000)
        self.assertEqual(cats["block_storage"][0]["storage_gb_month"], 10000)
        self.assertEqual(cats["egress"][0]["transfer_gb"], 20000)
        self.assertEqual(cats["licence"][0]["cost"], 529.92, "Windows vCPU-hours are a licence, not more capacity")

    def test_ibm_account_summary_csv(self):
        estate = self.ingest("ibm-account-summary.csv")
        cats = self.by_category(estate)
        self.assertEqual(estate["meta"]["input_kind"], ["ibm"])
        self.assertEqual(cats["support"][0]["cost"], 1000.0, "support summary section is part of the bill")
        self.assertEqual(sum(i.get("vcpu_hours", 0) for i in cats["compute"]),
                         (6 * 16 + 2 * 16 + 3 * 16) * 720 + 4 * 720)

    def test_ibm_focus(self):
        estate = self.ingest("ibm-focus.csv")
        self.assertEqual({i["cloud"] for i in estate["items"]}, {"ibm"})
        self.assertEqual(estate["meta"]["total_cost_ex_tax_ex_credits"], 7514.0)
        self.assertEqual(estate["meta"]["unclassified_top"], [])

    def test_alibaba_focus_is_cost_only(self):
        """Alibaba's FOCUS preview leaves quantities and SKUs null, so sizing is cost-implied and confidence low."""
        estate = self.ingest("alibaba-focus.csv")
        meta, cats = estate["meta"], self.by_category(estate)
        self.assertEqual({i["cloud"] for i in estate["items"]}, {"alibaba"})
        self.assertEqual(meta["detail_level"], "service")
        self.assertEqual(meta["unclassified_top"], [])
        for cat in ("compute", "database", "cache", "object_storage", "block_storage", "load_balancer", "egress", "cdn"):
            self.assertIn(cat, cats)
        plan = self.estimate()
        self.assertEqual(plan["confidence"]["grade"], "low")
        self.assertGreater(plan["confidence"]["share_of_sized_spend_cost_implied"], 0.5)


class BareMetalEstimateProviders(unittest.TestCase):
    """Catalogue integrity and provider selection."""

    SKILL = PLUGIN / "skills" / "counterweight-bare-metal-estimate"

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(cls.SKILL / "scripts"))
        import estimate  # noqa: E402
        import specs  # noqa: E402
        cls.estimate_mod, cls.specs = estimate, specs
        cls.catalogue = json.loads((cls.SKILL / "assets" / "catalogue.json").read_text(encoding="utf-8"))

    def run_estimate(self, estate, **overrides):
        a = dict(self.estimate_mod.DEFAULTS)
        a.update(overrides)
        return self.estimate_mod.estimate(estate, json.loads(json.dumps(self.catalogue)), a)

    @staticmethod
    def small_estate(ram_gb=32):
        return {"meta": {"period_hours": 730, "months_covered": 1.0, "currency": "EUR", "detail_level": "usage"},
                "items": [{"category": "compute", "cloud": "aws", "instance_type": "m6i.2xlarge", "usage_hours": 730 * 20,
                           "cost": 6000.0},
                          {"category": "compute", "cloud": "ibm", "instance_type": f"mx2-16x{ram_gb}", "usage_hours": 730,
                           "cost": 800.0},
                          {"category": "egress", "transfer_gb": 30000, "cost": 2500.0}]}

    def test_catalogue_entries_are_complete(self):
        fx = self.catalogue["fx"]
        self.assertTrue(fx["as_of"] and fx["source"])
        fb = self.catalogue["object_storage_fallback"]["provider"]
        self.assertTrue(self.catalogue["providers"][fb]["object_storage"])
        for key, p in self.catalogue["providers"].items():
            with self.subTest(provider=key):
                self.assertIsInstance(p["default_enabled"], bool)
                self.assertIsInstance(p["eu_uk_locations"], bool)
                self.assertRegex(p["prices_checked"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertTrue(p.get("pricing_caveat") or p["default_enabled"])
                for s in p["servers"]:
                    self.assertTrue(s["source"].startswith("https://"), s["model"])
                    self.assertGreater(s["monthly_eur"], 0)
                    self.assertGreaterEqual(s["threads"], s["cores"])
                    self.assertGreaterEqual(s["nvme_raw_gb"], s["nvme_gb"])
                    if s.get("gpu"):
                        self.assertIn(s["gpu"]["model"], self.estimate_mod.GPU_RANK)
                    lp = s.get("list_price")
                    if lp:
                        rate = fx[f"{lp['currency']}_EUR"]
                        self.assertAlmostEqual(s["monthly_eur"], lp["amount"] * rate, delta=lp["amount"] * 0.0005 + 0.01)

    def test_default_providers(self):
        defaults = self.estimate_mod.select_providers(self.catalogue)
        self.assertEqual(defaults, ["hetzner", "ovhcloud", "scaleway"])
        self.assertEqual(self.estimate_mod.select_providers(self.catalogue, ["default", "teraswitch"]),
                         defaults + ["teraswitch"])
        self.assertEqual(self.estimate_mod.select_providers(self.catalogue, ["all"]), list(self.catalogue["providers"]))
        for opt_in in ("leaseweb", "cherryservers", "latitude", "phoenixnap", "teraswitch", "vultr"):
            self.assertNotIn(opt_in, defaults)

    def test_opt_in_providers_are_priced_when_asked(self):
        plan = self.run_estimate(self.small_estate(), providers=["all"])
        self.assertEqual(set(plan["providers"]), set(self.catalogue["providers"]))
        for key, r in plan["providers"].items():
            with self.subTest(provider=key):
                self.assertGreater(r["scenarios"]["right_sized"]["server_count"], 0)

    def test_providers_without_object_storage_borrow_a_price(self):
        estate = self.small_estate()
        estate["items"].append({"category": "object_storage", "storage_gb_month": 20000, "cost": 460.0})
        plan = self.run_estimate(estate, providers=["cherryservers"])
        self.assertGreater(plan["providers"]["cherryservers"]["scenarios"]["right_sized"]["monthly_eur"]["object_storage"], 0)
        self.assertTrue(any("stand-in" in f["text"] for f in plan["flags"]))

    def test_unknown_provider_is_flagged(self):
        plan = self.run_estimate(self.small_estate(), providers=["hetzner", "nosuchhost"])
        self.assertIn("hetzner", plan["providers"])
        self.assertTrue(any("nosuchhost" in f["text"] for f in plan["flags"]))

    def test_recommendation_prefers_eu_and_uk_locations(self):
        cat = json.loads(json.dumps(self.catalogue))
        cat["providers"]["vultr"]["eu_uk_locations"] = False
        for s in cat["providers"]["vultr"]["servers"]:
            s["monthly_eur"] = 1.0  # cheapest by far, but outside Europe
        a = dict(self.estimate_mod.DEFAULTS, providers=["hetzner", "vultr"])
        plan = self.estimate_mod.estimate(self.small_estate(), cat, a)
        self.assertEqual(plan["recommended_provider"], "hetzner")
        self.assertTrue(any("no data centres in the EU or UK" in f["text"] for f in plan["flags"]))

    def test_provider_that_cannot_fit_the_largest_workload_is_not_recommended(self):
        plan = self.run_estimate(self.small_estate(ram_gb=128), providers=["hetzner", "ovhcloud"])
        self.assertFalse(plan["providers"]["ovhcloud"]["scenarios"]["right_sized"]["fits"])
        self.assertEqual(plan["recommended_provider"], "hetzner")

    def test_decoders_for_new_clouds(self):
        self.assertEqual((self.specs.decode("bx2-4x16")["vcpu"], self.specs.decode("bx2-4x16")["ram_gb"]), (4, 16))
        ali = self.specs.decode("ecs.g8ae.4xlarge")
        self.assertEqual((ali["cloud"], ali["vcpu"], ali["ram_gb"]), ("alibaba", 16, 64))
        self.assertIsNone(self.specs.decode("ecs.sn1ne.3xlarge"), "families without a documented ratio are not guessed")
        self.assertEqual(self.specs.oci_series("Compute - Standard - X12 Ax - OCPU"), "x12ax")
        self.assertEqual(self.specs.oci_traits("a1")[:2], (1, False))
        self.assertEqual(self.specs.oci_traits("e5")[:2], (2, True))


if __name__ == "__main__":
    unittest.main()
