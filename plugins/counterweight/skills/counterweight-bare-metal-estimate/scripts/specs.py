"""Decode cloud instance / machine type names into vCPU, RAM, GPU and CPU traits.

Pure standard library. Covers the naming schemes of AWS (EC2, RDS, ElastiCache,
OpenSearch, Redshift), Azure (VM sizes, incl. constrained vCPU) and Google Cloud
(predefined and custom machine types). Decoding is rule-based, so new sizes in an
existing family work without a table update; unusual families fall back to a
guess that is flagged with confidence="low".

Usage as a library:  from specs import decode
                     decode("m6i.2xlarge")  -> dict
Usage as a CLI:      python3 specs.py m6i.2xlarge Standard_E16-4s_v5 n2-highmem-8
"""
from __future__ import annotations

import json
import re
import sys

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Per-thread performance of a cloud vCPU relative to one hardware thread on a
# current bare-metal part (Zen 4/Zen 5 EPYC or Ryzen at 3.5-5.7 GHz). These are
# planning factors for the "right-sized" scenario only. Like-for-like sizing
# ignores them. Validate with a benchmark during a paid engagement.
GEN_FACTOR = {
    "legacy": 0.60,   # Haswell/Broadwell/Skylake era: AWS gen<=4, Azure v2/v3, GCP n1
    "older": 0.70,    # Cascade Lake / Rome: AWS gen 5, Azure v4, GCP n2/n2d/c2
    "recent": 0.85,   # Ice Lake / Milan / Graviton2: AWS gen 6, Azure v5
    "current": 1.00,  # Sapphire/Emerald/Granite Rapids, Genoa/Turin, Graviton3/4, Axion
}

# A vCPU that is a whole physical core (Arm, or x86 with SMT disabled) does more
# work than one SMT thread when both siblings are busy. Express it in
# "SMT thread equivalents" so all demand lands in one unit.
NO_SMT_THREAD_EQUIV = 1.4


def _result(**kw):
    base = {
        "name": None, "cloud": None, "family": None, "vcpu": None, "ram_gb": None,
        "gpu_count": 0, "gpu_model": None, "gpu_mem_gb": None, "arch": "x86_64",
        "smt": True, "burstable": False, "baseline_fraction": 1.0,
        "generation": "recent", "local_nvme_gb": 0, "confidence": "high", "notes": [],
    }
    base.update(kw)
    base["thread_equiv"] = None
    if base["vcpu"] is not None:
        per = 1.0 if base["smt"] else NO_SMT_THREAD_EQUIV
        base["thread_equiv"] = round(base["vcpu"] * per, 3)
    base["gen_factor"] = GEN_FACTOR.get(base["generation"], 0.85)
    return base


GPU_MEM = {"Inferentia": 8, "Inferentia2": 32, "Trainium": 32, "Trainium2": 96, "T4": 16, "A10G": 24, "A10": 24, "L4": 24, "L40S": 48, "V100": 16, "A100-40": 40,
           "A100-80": 80, "H100": 80, "H200": 141, "B200": 180, "K80": 12, "M60": 8,
           "T4G": 16, "P100": 16, "P4": 8, "A2": 16, "RTX-PRO-6000": 96, "MI300X": 192}

# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------

AWS_SIZE_VCPU = {"nano": 2, "micro": 2, "small": 2, "medium": 2, "large": 2, "xlarge": 4}

# GB of RAM per vCPU by family prefix (first letters before the generation digit)
AWS_RAM_PER_VCPU = {
    "m": 4, "c": 2, "r": 8, "x": 16, "z": 8, "i": 8, "d": 8, "h": 4, "g": 4, "p": 7.625,
    "a": 2, "im": 4, "is": 6, "hpc": 4, "u": 13.6, "inf": 2, "trn": 4, "dl": 8, "f": 15.25,
    "vt": 2, "mac": 4,
}

# Exceptions where a family deviates from the prefix ratio
AWS_FAMILY_RAM = {
    "x1": 15.25, "x1e": 30.5, "x2idn": 16, "x2iedn": 32, "x2iezn": 32, "x2gd": 16, "x8g": 16,
    "i3": 7.625, "i3en": 8, "i4i": 8, "i4g": 8, "i7ie": 8, "i8g": 8, "d2": 7.625, "d3": 8, "d3en": 4,
    "r3": 7.625, "r4": 7.625, "c4": 1.875, "m4": 4, "g4dn": 4, "g5": 4, "g6": 4, "g6e": 8,
    "g4ad": 4, "g5g": 2, "p3": 7.625, "p3dn": 8, "p4d": 12, "p4de": 12, "p5": 10.67,
    "p5e": 10.67, "p5en": 10.67, "z1d": 8, "hpc7g": 2, "hpc6a": 4, "c5n": 2.625, "r5b": 8,
    "m5zn": 4, "t2": 4, "t3": 4, "t3a": 4, "t4g": 4,
}

# t-family sizes are not proportional
AWS_T_SIZES = {  # size: (vcpu, ram_gb, baseline_fraction_per_vcpu)
    "nano": (2, 0.5, 0.05), "micro": (2, 1, 0.10), "small": (2, 2, 0.20), "medium": (2, 4, 0.20),
    "large": (2, 8, 0.30), "xlarge": (4, 16, 0.40), "2xlarge": (8, 32, 0.40),
}
AWS_T2_SIZES = {"nano": (1, 0.5, 0.05), "micro": (1, 1, 0.10), "small": (1, 2, 0.20),
                "medium": (2, 4, 0.20), "large": (2, 8, 0.30), "xlarge": (4, 16, 0.225),
                "2xlarge": (8, 32, 0.169)}

# GPU fit: family -> (gpu model, {size: gpu_count}); default 1
AWS_GPU = {
    "g4dn": ("T4", {"12xlarge": 4, "metal": 8}),
    "g4ad": ("Radeon-V520", {"8xlarge": 2, "16xlarge": 4}),
    "g5": ("A10G", {"12xlarge": 4, "24xlarge": 4, "48xlarge": 8}),
    "g5g": ("T4G", {"16xlarge": 2, "metal": 2}),
    "g6": ("L4", {"12xlarge": 4, "24xlarge": 4, "48xlarge": 8}),
    "g6e": ("L40S", {"12xlarge": 4, "24xlarge": 4, "48xlarge": 8}),
    "gr6": ("L4", {}),
    "p3": ("V100", {"2xlarge": 1, "8xlarge": 4, "16xlarge": 8}),
    "p3dn": ("V100", {"24xlarge": 8}),
    "p4d": ("A100-40", {"24xlarge": 8}),
    "p4de": ("A100-80", {"24xlarge": 8}),
    "p5": ("H100", {"4xlarge": 1, "48xlarge": 8}),
    "p5e": ("H200", {"48xlarge": 8}),
    "p5en": ("H200", {"48xlarge": 8}),
    "p6-b200": ("B200", {"48xlarge": 8}),
    # AWS silicon: no bare-metal equivalent; the estimate keeps these in the cloud
    "inf1": ("Inferentia", {"xlarge": 1, "2xlarge": 1, "6xlarge": 4, "24xlarge": 16}),
    "inf2": ("Inferentia2", {"xlarge": 1, "8xlarge": 1, "24xlarge": 6, "48xlarge": 12}),
    "trn1": ("Trainium", {"2xlarge": 1, "32xlarge": 16}),
    "trn1n": ("Trainium", {"32xlarge": 16}),
    "trn2": ("Trainium2", {"48xlarge": 16}),
}

# Fixed shapes that don't follow size multipliers
AWS_FIXED = {
    "p3.2xlarge": (8, 61), "p3.8xlarge": (32, 244), "p3.16xlarge": (64, 488),
    "p4d.24xlarge": (96, 1152), "p4de.24xlarge": (96, 1152), "p5.48xlarge": (192, 2048),
    "p5.4xlarge": (16, 256), "p5e.48xlarge": (192, 2048), "p5en.48xlarge": (192, 2048),
    "x1.16xlarge": (64, 976), "x1.32xlarge": (128, 1952),
    "ra3.xlplus": (4, 32), "ra3.large": (2, 16), "ra3.4xlarge": (12, 96), "ra3.16xlarge": (48, 384),
    "dc2.large": (2, 15), "dc2.8xlarge": (32, 244),
    "g4dn.metal": (96, 384), "u-6tb1.metal": (448, 6144), "u-12tb1.metal": (448, 12288),
    "m3.medium": (1, 3.75), "m3.large": (2, 7.5), "m3.xlarge": (4, 15), "m3.2xlarge": (8, 30),
    "c4.8xlarge": (36, 60), "c3.8xlarge": (32, 60), "r3.8xlarge": (32, 244), "m4.10xlarge": (40, 160),
    "m4.16xlarge": (64, 256), "c5.9xlarge": (36, 72), "c5.18xlarge": (72, 144),
    "inf1.xlarge": (4, 8), "inf1.2xlarge": (8, 16), "inf1.6xlarge": (24, 48), "inf1.24xlarge": (96, 192),
    "inf2.xlarge": (4, 16), "inf2.8xlarge": (32, 128), "inf2.24xlarge": (96, 384), "inf2.48xlarge": (192, 768),
    "trn1.2xlarge": (8, 32), "trn1.32xlarge": (128, 512), "trn1n.32xlarge": (128, 512),
}

# AMD EPYC families from the 7th generation run with SMT disabled: every vCPU is a physical core
AWS_NO_SMT = {"m7a", "c7a", "r7a", "hpc7a", "m8a", "c8a", "r8a", "x8a"}

AWS_METAL_VCPU = {  # family -> vCPU of .metal size
    "m5": 96, "m5d": 96, "m5zn": 48, "c5": 96, "c5d": 96, "c5n": 72, "r5": 96, "r5d": 96,
    "m6i": 128, "c6i": 128, "r6i": 128, "m6a": 192, "c6a": 192, "r6a": 192, "m6g": 64,
    "c6g": 64, "r6g": 64, "m7i": 192, "c7i": 192, "r7i": 192, "m7a": 192, "c7a": 192,
    "r7a": 192, "m7g": 64, "c7g": 64, "r7g": 64, "m8g": 192, "c8g": 192, "r8g": 192,
    "i3": 72, "i4i": 128, "i3en": 96, "z1d": 48, "x2idn": 128, "x2iedn": 128,
}

# Families whose generation digit doesn't line up with the m/c/r cadence
AWS_FAMILY_GEN = {
    "x1": "legacy", "x1e": "legacy", "x2idn": "recent", "x2iedn": "recent", "x2iezn": "older",
    "x2gd": "recent", "i3": "legacy", "i3en": "older", "i4i": "recent", "i4g": "recent",
    "im4gn": "recent", "is4gen": "recent", "g4dn": "older", "g4ad": "older", "g5": "recent",
    "g5g": "recent", "g6": "current", "g6e": "current", "gr6": "current", "p3": "legacy",
    "p3dn": "older", "p4d": "recent", "p4de": "recent", "p5": "current", "p5e": "current",
    "p5en": "current", "z1d": "older", "d2": "legacy", "d3": "older", "d3en": "older",
    "ra3": "older", "dc2": "legacy", "h1": "legacy", "u": "older", "hpc6a": "recent",
    "inf1": "older", "inf2": "current", "trn1": "current", "trn1n": "current", "trn2": "current",
    "m3": "legacy", "c3": "legacy", "r3": "legacy",
}

AWS_RE = re.compile(r"^(?P<fam>[a-z]+(?:-[a-z0-9]+)?(?P<gen>\d+)?(?P<suf>[a-z\-]*))\.(?P<size>\d*(?:nano|micro|small|medium|large|xlarge|xlplus)|metal(?:-\d+xl)?)$")


def _aws_generation(prefix: str, gen: int, arm: bool) -> str:
    if arm:
        return "recent" if gen <= 6 else "current"
    if gen <= 4:
        return "legacy"
    if gen == 5:
        return "older"
    if gen == 6:
        return "recent"
    return "current"


def decode_aws(name: str) -> dict | None:
    raw = name
    n = name.strip().lower()
    for pre in ("db.", "cache.", "ml.", "kafka.", "mq.", "dax.", "docdb.", "neptune."):
        if n.startswith(pre):
            n = n[len(pre):]
    n = re.sub(r"\.(search|elasticsearch)$", "", n)
    n = n.replace("-flex", "")
    m = AWS_RE.match(n)
    if not m:
        return None
    fam, size = m.group("fam"), m.group("size")
    pm = re.match(r"^([a-z]+?)(\d+)([a-z\-]*)$", fam)
    if not pm:
        if n in AWS_FIXED:
            v, r = AWS_FIXED[n]
            return _result(name=raw, cloud="aws", family=fam, vcpu=v, ram_gb=r,
                           generation=AWS_FAMILY_GEN.get(fam, "recent"))
        return None
    prefix, gen, suffix = pm.group(1), int(pm.group(2)), pm.group(3)
    arm = "g" in suffix or fam == "a1"
    no_smt = arm or fam in AWS_NO_SMT
    notes = []
    gen_label = AWS_FAMILY_GEN.get(fam) or _aws_generation(prefix, gen, arm)

    # burstable t family
    if prefix == "t":
        table = AWS_T2_SIZES if fam == "t2" else AWS_T_SIZES
        if size not in table:
            return None
        v, r, base = table[size]
        notes.append(f"burstable; baseline {int(base*100)}% per vCPU")
        return _result(name=raw, cloud="aws", family=fam, vcpu=v, ram_gb=r, arch="arm64" if arm else "x86_64",
                       smt=not arm, burstable=True, baseline_fraction=base, generation=gen_label, notes=notes)

    if n in AWS_FIXED:
        vcpu, ram = AWS_FIXED[n]
    else:
        if size.startswith("metal"):
            vcpu = AWS_METAL_VCPU.get(fam)
            if vcpu is None:
                vcpu = 96
                notes.append("metal size assumed 96 vCPU")
        elif size == "medium" and (arm or prefix == "a" or fam in AWS_NO_SMT):
            vcpu = 1
        elif size in AWS_SIZE_VCPU:
            vcpu = AWS_SIZE_VCPU[size]
        else:
            mult = re.match(r"^(\d+)xlarge$", size)
            if not mult:
                return None
            vcpu = 4 * int(mult.group(1))
        ratio = AWS_FAMILY_RAM.get(fam) or AWS_FAMILY_RAM.get(prefix + str(gen))
        if ratio is None:
            ratio = AWS_RAM_PER_VCPU.get(prefix)
        if ratio is None:
            ratio = 4
            notes.append("unknown family, assumed 4 GB per vCPU")
        ram = round(vcpu * ratio, 2)

    gpu_model, gpu_count = None, 0
    if fam in AWS_GPU:
        gpu_model, sizes = AWS_GPU[fam]
        gpu_count = sizes.get(size, 1)
    local_nvme = 0
    if "d" in suffix or prefix in ("i", "im", "is") or fam in ("d3", "d3en"):
        notes.append("instance store present")
    conf = "high" if not notes or all(x.startswith(("burstable", "instance store")) for x in notes) else "medium"
    return _result(name=raw, cloud="aws", family=fam, vcpu=vcpu, ram_gb=ram, gpu_count=gpu_count,
                   gpu_model=gpu_model, gpu_mem_gb=GPU_MEM.get(gpu_model) if gpu_model else None,
                   arch="arm64" if arm else "x86_64", smt=not no_smt, generation=gen_label,
                   local_nvme_gb=local_nvme, confidence=conf, notes=notes)

# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------

AZURE_RE = re.compile(
    r"^(?:standard_|basic_|gp_standard_|mo_standard_|bc_standard_)?"
    r"(?P<fam>[a-z]+?)(?P<vcpu>\d+)(?:-(?P<active>\d+))?(?P<feat>[a-z]*)"
    r"(?:_(?P<gpu>(?!v\d)[a-z0-9]+))?(?:_v(?P<ver>\d+))?(?:_promo)?$")

AZURE_RAM = {  # family -> GB per vCPU (for v3+ unless overridden by version)
    "a": 2, "b": 4, "d": 4, "ds": 4, "dc": 4, "e": 8, "ec": 8, "f": 2, "fx": 21, "g": 14, "h": 7,
    "hb": 4, "hc": 8, "l": 8, "m": 14, "nc": 7, "nd": 19, "nv": 7, "ng": 2, "dl": 2,
}
AZURE_B_FIXED = {  # B-series v1 shapes: vcpu, ram, baseline per vCPU
    "b1ls": (1, 0.5, 0.05), "b1s": (1, 1, 0.10), "b1ms": (1, 2, 0.20), "b2s": (2, 4, 0.20),
    "b2ms": (2, 8, 0.30), "b4ms": (4, 16, 0.225), "b8ms": (8, 32, 0.169), "b12ms": (12, 48, 0.15),
    "b16ms": (16, 64, 0.15), "b20ms": (20, 80, 0.15),
}
AZURE_GPU = {  # (family, gpu token or feature) -> model, gpus per vCPU unit
    "nc_t4": ("T4", 4), "nc_a100": ("A100-80", 24), "nc_h100": ("H100", 40), "nc_v100": ("V100", 6),
    "nd_a100": ("A100-40", 12), "nd_h100": ("H100", 12), "nv_a10": ("A10", 36), "nc_a10": ("A10", 8),
}


# Sizes whose name does not encode vCPU and RAM: v1/v2 D/DS/G/A, M-series, GPU families.
# key: lower-case size without "standard_" -> (vcpu, ram_gb, gpu_count, gpu_model, generation)
AZURE_TABLE = {}
for _p in ("d", "ds"):
    for _v in ("", "_v2"):
        _g = "legacy"
        for _n, (_c, _r) in {"1": (1, 3.5), "2": (2, 7), "3": (4, 14), "4": (8, 28), "5": (16, 56),
                              "11": (2, 14), "12": (4, 28), "13": (8, 56), "14": (16, 112), "15": (20, 140)}.items():
            if _v == "" and _n in ("5", "15"):
                continue
            AZURE_TABLE[f"{_p}{_n}{_v}"] = (_c, _r, 0, None, _g)
for _n, (_c, _r) in {"1": (2, 28), "2": (4, 56), "3": (8, 112), "4": (16, 224), "5": (32, 448)}.items():
    AZURE_TABLE[f"g{_n}"] = AZURE_TABLE[f"gs{_n}"] = (_c, _r, 0, None, "legacy")
AZURE_TABLE.update({
    "a1_v2": (1, 2, 0, None, "legacy"), "a2_v2": (2, 4, 0, None, "legacy"), "a4_v2": (4, 8, 0, None, "legacy"),
    "a8_v2": (8, 16, 0, None, "legacy"), "a2m_v2": (2, 16, 0, None, "legacy"), "a4m_v2": (4, 32, 0, None, "legacy"),
    "a8m_v2": (8, 64, 0, None, "legacy"),
    "m8ms": (8, 218.75, 0, None, "older"), "m16ms": (16, 437.5, 0, None, "older"), "m32ts": (32, 192, 0, None, "older"),
    "m32ls": (32, 256, 0, None, "older"), "m32ms": (32, 875, 0, None, "older"), "m64s": (64, 1024, 0, None, "older"),
    "m64ls": (64, 512, 0, None, "older"), "m64ms": (64, 1792, 0, None, "older"), "m128s": (128, 2048, 0, None, "older"),
    "m128ms": (128, 3892, 0, None, "older"), "m208s_v2": (208, 2850, 0, None, "older"),
    "m208ms_v2": (208, 5700, 0, None, "older"), "m416s_v2": (416, 5700, 0, None, "older"),
    "m416ms_v2": (416, 11400, 0, None, "older"),
    # GPU
    "nc6s_v3": (6, 112, 1, "V100", "older"), "nc12s_v3": (12, 224, 2, "V100", "older"),
    "nc24s_v3": (24, 448, 4, "V100", "older"), "nc24rs_v3": (24, 448, 4, "V100", "older"),
    "nc4as_t4_v3": (4, 28, 1, "T4", "recent"), "nc8as_t4_v3": (8, 56, 1, "T4", "recent"),
    "nc16as_t4_v3": (16, 110, 1, "T4", "recent"), "nc64as_t4_v3": (64, 440, 4, "T4", "recent"),
    "nc24ads_a100_v4": (24, 220, 1, "A100-80", "current"), "nc48ads_a100_v4": (48, 440, 2, "A100-80", "current"),
    "nc96ads_a100_v4": (96, 880, 4, "A100-80", "current"),
    "nc40ads_h100_v5": (40, 320, 1, "H100", "current"), "nc80adis_h100_v5": (80, 640, 2, "H100", "current"),
    "nd96asr_v4": (96, 900, 8, "A100-40", "current"), "nd96amsr_a100_v4": (96, 1900, 8, "A100-80", "current"),
    "nd96isr_h100_v5": (96, 1900, 8, "H100", "current"), "nd96isr_h200_v5": (96, 1850, 8, "H200", "current"),
    "nv12s_v3": (12, 112, 1, "M60", "older"), "nv24s_v3": (24, 224, 2, "M60", "older"),
    "nv48s_v3": (48, 448, 4, "M60", "older"),
    "nv6ads_a10_v5": (6, 55, 1 / 6, "A10", "recent"), "nv12ads_a10_v5": (12, 110, 1 / 3, "A10", "recent"),
    "nv18ads_a10_v5": (18, 220, 0.5, "A10", "recent"), "nv36ads_a10_v5": (36, 440, 1, "A10", "recent"),
    "nv36adms_a10_v5": (36, 880, 1, "A10", "recent"), "nv72ads_a10_v5": (72, 880, 2, "A10", "recent"),
})


def decode_azure(name: str) -> dict | None:
    raw = name
    n = name.strip().lower().replace(" ", "_")
    key = re.sub(r"^(standard_|basic_|gp_standard_|mo_standard_|bc_standard_)", "", n).replace("_promo", "")
    if key in AZURE_TABLE:
        v, r, g, gm, gen = AZURE_TABLE[key]
        notes = []
        if g and g < 1:
            notes.append(f"fractional GPU: {g:.2f} of one {gm}")
        return _result(name=raw, cloud="azure", family=key.upper(), vcpu=v, ram_gb=r, gpu_count=g, gpu_model=gm,
                       gpu_mem_gb=(GPU_MEM.get(gm) * min(1, g)) if gm and GPU_MEM.get(gm) else None,
                       generation=gen, notes=notes)
    m = AZURE_RE.match(n)
    if not m:
        return None
    fam, vcpu, active, feat, gputok, ver = (m.group("fam"), int(m.group("vcpu")), m.group("active"),
                                            m.group("feat"), m.group("gpu"), m.group("ver"))
    ver = int(ver) if ver else 1
    notes = []
    arm = "p" in feat
    full = f"{fam}{vcpu}{feat}"
    if fam == "b" and ver == 1 and full in AZURE_B_FIXED:
        v, r, base = AZURE_B_FIXED[full]
        return _result(name=raw, cloud="azure", family="B", vcpu=v, ram_gb=r, burstable=True,
                       baseline_fraction=base, generation="older", notes=[f"burstable; baseline {int(base*100)}%"])
    ratio = AZURE_RAM.get(fam)
    if ratio is None and fam[:1] in AZURE_RAM:
        ratio = AZURE_RAM[fam[:1]]
    if ratio is None:
        return None
    if fam == "d" and ver <= 2:
        ratio = 3.5
    if fam == "f" and ver == 1:
        ratio = 2
    if fam == "e" and ver <= 2:
        ratio = 8
    if "l" in feat and fam in ("d", "e"):
        ratio = 2
    if "m" in feat and fam in ("d", "b"):
        ratio = ratio * 2 if fam == "d" else 8
    if fam == "b" and ver >= 2:
        ratio = 2 if "l" in feat else (8 if "m" in feat else 4)
    if fam == "m":
        ratio = 14 if "m" not in feat else 28
    if fam == "nc" and gputok == "t4":
        ratio = 7  # NC4as_T4_v3: 4 vCPU, 28 GB
    if fam == "nc" and gputok == "a100":
        ratio = 9.17  # NC24ads_A100_v4: 24 vCPU, 220 GB
    if fam == "nc" and gputok == "h100":
        ratio = 8  # NC40ads_H100_v5: 40 vCPU, 320 GB
    ram = round(vcpu * ratio, 2)
    gpu_model, gpu_count = None, 0
    if fam in ("nc", "nd", "nv"):
        key = f"{fam}_{gputok}" if gputok else None
        if key and key in AZURE_GPU:
            gpu_model, per = AZURE_GPU[key]
            gpu_count = max(1, round(vcpu / per))
        else:
            gpu_model, gpu_count = "unknown", 1
            notes.append("GPU model not decoded")
    burst = fam == "b"
    gen = {1: "legacy", 2: "legacy", 3: "legacy", 4: "older", 5: "recent"}.get(ver, "current")
    if fam == "l":
        gen = {2: "older", 3: "recent"}.get(ver, gen)
    if fam == "b" and ver >= 2:
        gen = "recent"
    if arm:
        gen = "recent" if ver <= 5 else "current"
    eff_vcpu = int(active) if active else vcpu
    if active:
        notes.append(f"constrained size: {active} active vCPU of {vcpu}")
    return _result(name=raw, cloud="azure", family=fam.upper() + (feat or "") + f"_v{ver}", vcpu=eff_vcpu,
                   ram_gb=ram, gpu_count=gpu_count, gpu_model=gpu_model,
                   gpu_mem_gb=GPU_MEM.get(gpu_model) if gpu_model else None,
                   arch="arm64" if arm else "x86_64", smt=not arm, burstable=burst,
                   baseline_fraction=0.4 if burst else 1.0, generation=gen,
                   confidence="high" if not notes else "medium", notes=notes)

# ---------------------------------------------------------------------------
# Google Cloud
# ---------------------------------------------------------------------------

GCP_TYPE_RATIO = {"standard": 4, "highmem": 8, "highcpu": 1, "megamem": 14.9, "ultramem": 24,
                  "hypermem": 15.75, "lssd": 4}
GCP_SERIES_OVERRIDE = {("n1", "standard"): 3.75, ("n1", "highmem"): 6.5, ("n1", "highcpu"): 0.9,
                       ("c3", "highcpu"): 2, ("c3d", "highcpu"): 2, ("c4", "highcpu"): 2,
                       ("c4a", "highcpu"): 2, ("n4", "highcpu"): 2, ("c2", "standard"): 4,
                       ("c2d", "highcpu"): 2, ("m1", "megamem"): 14.93, ("m1", "ultramem"): 24.02,
                       ("m3", "megamem"): 15.25, ("m3", "ultramem"): 30.5}
GCP_SHARED = {"e2-micro": (2, 1, 0.125), "e2-small": (2, 2, 0.25), "e2-medium": (2, 4, 0.5),
              "f1-micro": (1, 0.6, 0.2), "g1-small": (1, 1.7, 0.5)}
GCP_GPU_SERIES = {"a2": "A100-40", "a2-ultragpu": "A100-80", "a3": "H100", "a3-ultragpu": "H200",
                  "g2": "L4", "a4": "B200"}
GCP_A2 = {"a2-highgpu-1g": (12, 85, 1), "a2-highgpu-2g": (24, 170, 2), "a2-highgpu-4g": (48, 340, 4),
          "a2-highgpu-8g": (96, 680, 8), "a2-megagpu-16g": (96, 1360, 16),
          "a2-ultragpu-1g": (12, 170, 1), "a2-ultragpu-8g": (96, 1360, 8),
          "a3-highgpu-8g": (208, 1872, 8), "a3-megagpu-8g": (208, 1872, 8),
          "a3-ultragpu-8g": (224, 2952, 8)}
GCP_FIXED = {"m2-ultramem-208": (208, 5888), "m2-ultramem-416": (416, 11776), "m2-megamem-416": (416, 5888),
             "m2-hypermem-416": (416, 8832), "m1-ultramem-40": (40, 961), "m1-ultramem-80": (80, 1922),
             "m1-ultramem-160": (160, 3844), "m1-megamem-96": (96, 1433.6)}


def decode_gcp(name: str) -> dict | None:
    raw = name
    n = name.strip().lower()
    n = n.split("/")[-1]
    n = re.sub(r"^db-", "", n)  # Cloud SQL tiers: db-custom-4-16384, db-n1-standard-4
    if n in GCP_SHARED:
        v, r, base = GCP_SHARED[n]
        return _result(name=raw, cloud="gcp", family=n.split("-")[0], vcpu=v, ram_gb=r, burstable=True,
                       baseline_fraction=base, generation="older", notes=["shared-core"])
    if n in GCP_FIXED:
        v, r = GCP_FIXED[n]
        return _result(name=raw, cloud="gcp", family=n.split("-")[0], vcpu=v, ram_gb=r, generation="older")
    if n in GCP_A2:
        v, r, g = GCP_A2[n]
        series = "a2-ultragpu" if "ultragpu" in n and n.startswith("a2") else (
            "a3-ultragpu" if "ultragpu" in n else n.split("-")[0])
        model = GCP_GPU_SERIES.get(series, "unknown")
        return _result(name=raw, cloud="gcp", family=series, vcpu=v, ram_gb=r, gpu_count=g, gpu_model=model,
                       gpu_mem_gb=GPU_MEM.get(model), generation="current")
    cm = re.match(r"^(?:(?P<series>[a-z0-9]+)-)?custom-(?P<v>\d+)-(?P<mb>\d+)(?:-ext)?$", n)
    if cm:
        series = cm.group("series") or "n1"
        v, ram = int(cm.group("v")), round(int(cm.group("mb")) / 1024, 2)
        return _result(name=raw, cloud="gcp", family=series, vcpu=v, ram_gb=ram,
                       arch="arm64" if series in ("t2a", "c4a") else "x86_64",
                       smt=series not in ("t2a", "c4a", "t2d"), generation=_gcp_gen(series))
    pm = re.match(r"^(?P<series>[a-z][a-z0-9]*)-(?P<type>[a-z]+)-(?P<v>\d+)(?:-lssd|-metal)?$", n)
    if not pm:
        return None
    series, typ, v = pm.group("series"), pm.group("type"), int(pm.group("v"))
    ratio = GCP_SERIES_OVERRIDE.get((series, typ), GCP_TYPE_RATIO.get(typ))
    if ratio is None:
        return None
    gpu_model, gpu_count = None, 0
    if series == "g2":
        gpu_model = "L4"
        gpu_count = {4: 1, 8: 1, 12: 1, 16: 1, 24: 2, 32: 1, 48: 4, 96: 8}.get(v, 1)
    arm = series in ("t2a", "c4a")
    no_smt = arm or series == "t2d"
    return _result(name=raw, cloud="gcp", family=series, vcpu=v, ram_gb=round(v * ratio, 2),
                   gpu_count=gpu_count, gpu_model=gpu_model, gpu_mem_gb=GPU_MEM.get(gpu_model) if gpu_model else None,
                   arch="arm64" if arm else "x86_64", smt=not no_smt, generation=_gcp_gen(series))


def _gcp_gen(series: str) -> str:
    if series in ("n1", "f1", "g1"):
        return "legacy"
    if series in ("n2", "n2d", "c2", "c2d", "e2", "m1", "m2"):
        return "older"
    if series in ("t2d", "t2a", "m3", "a2"):
        return "recent"
    return "current"

# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Oracle Cloud (OCI)
# ---------------------------------------------------------------------------
# OCI bills flexible shapes per OCPU-hour and per GB-hour of memory, so the cost report
# carries the shape series ("Standard - E4", "Standard - A1 - Memory"), not a shape name.
# OCI's definitions: 1 OCPU on x86 = 2 vCPUs (one core, two threads); on Ampere A1 =
# 1 core; on Ampere A2 and A4 = 2 cores. Arm cores have no SMT.
# series -> (vCPU per OCPU, smt, generation)
OCI_SERIES = {
    "a1": (1, False, "recent"),     # Ampere Altra (Neoverse N1, Graviton2 class)
    "a2": (2, False, "current"),    # AmpereOne
    "a4": (2, False, "current"),    # AmpereOne M
    "a4ax": (2, False, "current"),
    "b1": (2, True, "legacy"), "x5": (2, True, "legacy"), "x7": (2, True, "legacy"),
    "e2": (2, True, "legacy"),      # EPYC 7551 (Naples)
    "e3": (2, True, "older"),       # EPYC 7742 (Rome)
    "e4": (2, True, "recent"),      # EPYC 7J13 (Milan)
    "e5": (2, True, "current"),     # EPYC 9J14 (Genoa)
    "e6": (2, True, "current"), "e6ax": (2, True, "current"),  # EPYC 9J45 (Turin)
    "x9": (2, True, "recent"),      # Xeon Platinum 8358 / 6354 (Ice Lake)
    "x12ax": (2, True, "current"),  # Xeon 6987P-C (Granite Rapids)
}
OCI_SERIES_RE = re.compile(
    r"\b(?:standard|optimized|dense\s*i/?o|hpc)\s*-\s*(?:hpc\s*-\s*)?([abex]\d{1,2}(?:\s*ax)?)\b", re.I)


def oci_series(text: str) -> str | None:
    """'Standard - E4 - Memory' -> 'e4'; 'Compute - Standard - X12 Ax - OCPU' -> 'x12ax'."""
    m = OCI_SERIES_RE.search(text or "")
    return re.sub(r"\s+", "", m.group(1).lower()) if m else None


def oci_traits(series: str | None) -> tuple:
    """(vCPU per OCPU, smt, generation) for an OCI series; unknown series are treated as recent x86."""
    return OCI_SERIES.get((series or "").lower(), (2, True, "recent"))


# ---------------------------------------------------------------------------
# IBM Cloud VPC profiles: <family><arch><generation>[d][c]-<vCPU>x<GiB>, e.g. bx2-4x16
# ---------------------------------------------------------------------------

IBM_RE = re.compile(r"^([nbcmuvgofh])([xz])([0-9f])(d)?(c)?-(\d+)x(\d+)$")
IBM_GEN = {"2": "older", "3": "current", "4": "current", "f": "recent"}  # gen 2 Cascade Lake, 3 Sapphire Rapids


def decode_ibm(name: str) -> dict | None:
    m = IBM_RE.match(name.strip().lower())
    if not m:
        return None
    fam, arch, gen, inst, _conf, vcpu, ram = m.groups()
    return _result(name=name, cloud="ibm", family=fam + arch + gen, vcpu=int(vcpu), ram_gb=float(ram),
                   arch="s390x" if arch == "z" else "x86_64", generation=IBM_GEN.get(gen, "recent"),
                   notes=["instance storage"] if inst else [])


# ---------------------------------------------------------------------------
# Alibaba Cloud ECS: ecs.<family>.<size>, e.g. ecs.g8ae.4xlarge = 16 vCPU, 64 GiB.
# Size gives vCPU (large = 2, xlarge = 4, Nxlarge = 4N); RAM per vCPU is only implied by the
# family letter (c 1:2, g 1:4, r 1:8), so other families are not decoded.
# ---------------------------------------------------------------------------

ALI_RE = re.compile(r"^ecs\.(?:hf)?([cgr])(\d+)([a-z]*)\.(\d*)(large|xlarge)$")
ALI_RAM = {"c": 2, "g": 4, "r": 8}


def decode_alibaba(name: str) -> dict | None:
    m = ALI_RE.match(name.strip().lower())
    if not m:
        return None
    fam, gen, suf, mult, size = m.groups()
    vcpu = 2 if size == "large" else 4 * (int(mult) if mult else 1)
    g = int(gen)
    arm = "y" in suf  # g8y, c8y, r8y: Yitian 710 (Arm)
    return _result(name=name, cloud="alibaba", family=f"{fam}{gen}{suf}", vcpu=vcpu, ram_gb=float(vcpu * ALI_RAM[fam]),
                   arch="arm64" if arm else "x86_64", smt=not arm,
                   generation="current" if g >= 8 else ("recent" if g == 7 else ("older" if g == 6 else "legacy")),
                   confidence="medium", notes=["RAM implied by the family ratio"])


def decode(name: str, cloud: str | None = None) -> dict | None:
    """Decode an instance/size name. cloud may be 'aws', 'azure', 'gcp', 'ibm', 'alibaba' or None (auto)."""
    if not name:
        return None
    s = name.strip()
    order = {"aws": [decode_aws], "azure": [decode_azure], "gcp": [decode_gcp], "ibm": [decode_ibm],
             "alibaba": [decode_alibaba]}.get((cloud or "").lower(), None)
    low = s.lower()
    if low.startswith("ecs."):
        order = [decode_alibaba]
    if order is None:
        if IBM_RE.match(low):
            order = [decode_ibm]
        elif low.startswith(("standard_", "basic_", "gp_standard", "bc_standard", "mo_standard")):
            order = [decode_azure]
        elif re.match(r"^[a-z0-9]+-(standard|highmem|highcpu|megamem|ultramem|custom|micro|small|medium|highgpu|megagpu|ultragpu|hypermem)", low) or "custom-" in low:
            order = [decode_gcp]
        elif "." in low:
            order = [decode_aws]
        else:
            order = [decode_azure, decode_gcp]
    for fn in order:
        try:
            r = fn(s)
        except Exception:  # never let one odd name break a whole bill
            r = None
        if r:
            return r
    return None


INSTANCE_TOKEN = re.compile(
    r"\b((?:db\.|cache\.|ml\.|ecs\.)?[a-z]{1,4}\d{1,2}[a-z\-]*\.(?:\d*(?:nano|micro|small|medium|large|xlarge|xlplus)|metal(?:-\d+xl)?)(?:\.search)?)\b"
    r"|\b(Standard_[A-Za-z0-9\-]+(?:_v\d+)?)\b"
    r"|\b((?:n1|n2|n2d|n4|e2|c2|c2d|c3|c3d|c4|c4a|c4d|t2d|t2a|m1|m2|m3|a2|a3|g2|h3|z3)-(?:standard|highmem|highcpu|megamem|ultramem|custom|highgpu|megagpu|ultragpu)-?\d*(?:-\d+)?)\b",
    re.IGNORECASE)


AZURE_METER = re.compile(r"\b([A-Z]{1,3}\d{1,3}(?:-\d+)?[a-z]*)(?:[ _](?:v(\d)))?(?:\b|$)")


def find_instance_type(text: str, cloud: str | None = None) -> str | None:
    """Pull the first instance-type-looking token out of free text (usage type, description).

    For Azure meter names such as "D4s v5" or "E16-4as v5 Low Priority" pass cloud="azure".
    """
    if not text:
        return None
    if (cloud or "").lower() == "azure" and "standard_" not in text.lower():
        for m in AZURE_METER.finditer(text):
            tok, ver = m.group(1), m.group(2)
            cand = f"Standard_{tok}" + (f"_v{ver}" if ver else "")
            if decode_azure(cand):
                return cand
        return None
    m = INSTANCE_TOKEN.search(text)
    if not m:
        return None
    return next(g for g in m.groups() if g)


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["m6i.2xlarge", "db.r6g.xlarge", "t3.medium", "Standard_E16-4s_v5",
                                 "Standard_D4s_v5", "n2-highmem-8", "e2-medium", "g5.12xlarge",
                                 "n2-custom-6-24576", "cache.r7g.large", "Standard_NC4as_T4_v3"]:
        print(json.dumps(decode(arg), indent=None))
