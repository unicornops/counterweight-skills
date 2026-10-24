# Certifications: when the provider must be certified

Many companies cannot use a hosting provider that lacks an ISO 27001 certificate or a SOC 2 Type II report, however
cheap it is. An estimate that recommends one is no use to them. Find out early, pass the requirement to
`estimate.py`, and the recommended provider will hold it.

This is context for an estimate, not legal or audit advice. The company's auditor or compliance lead has the final
word on what they accept.

## Does the company need one?

Ask, in the step 1 message: "Does a new hosting provider need to hold ISO 27001 or a SOC 2 Type II report?" Most
people know. If they don't, these are the usual reasons the answer is yes:

- **They are certified themselves.** A company with ISO 27001 must manage its suppliers' security (Annex A controls
  5.19 to 5.22), and a SOC 2 audit tests vendor management. For the supplier that runs every production server,
  auditors almost always expect that supplier's own ISO 27001 certificate or SOC 2 Type II report. This is the most
  common case.
- **Their customers require it.** Enterprise security questionnaires, contracts and data processing agreements often
  require subprocessors with ISO 27001 or SOC 2. The new provider will usually also be a new subprocessor that the
  company has to tell customers about.
- **Their regulator expects it.** Financial entities under DORA must assess ICT third-party providers before
  contracting (Article 28). DORA names no certification, but ISO 27001 or SOC 2 is the usual evidence. Entities under
  NIS2 must manage supply-chain security. Health data in France needs an HDS-certified host. Card data needs the
  provider's PCI DSS attestation for the physical security it runs.

If they hold ISO 27001 or SOC 2 themselves and can't say which one their auditor accepts, require either one
(`iso27001|soc2`) and say in the summary that this was assumed. If they say "SOC 2", ask whether ISO 27001 would
also do. Few European bare-metal providers have SOC 2 reports, so accepting ISO 27001 widens the choice and usually
the saving. With no reason to require one, leave the requirement unset: the report still shows what each provider
holds.

## Passing it to the estimate

```bash
python3 scripts/estimate.py estate.json -o plan.json --require-cert "iso27001|soc2"
python3 scripts/estimate.py estate.json -o plan.json --require-cert soc2 --require-cert pci-dss
```

The keys are `iso27001`, `soc2` (a SOC 2 Type II report; Type I does not count), `soc1`, `pci-dss`, `iso27017`,
`iso27018` and `iso27701`. Use `|` when any one of several will do. Repeat the option, or separate entries with
commas, when all are needed. In `assumptions.json`, use `"required_certifications": ["iso27001|soc2"]`.

With a requirement set:

- **Which providers count.** A provider meets the requirement only where the catalogue records the certification
  for its dedicated or bare-metal servers, at a location where every required certification applies.
- **The recommendation.** It is the cheapest right-sized provider that meets the requirement and can hold the
  largest workload. Providers in the EU or UK come first, then providers that are priced by default.
- **Opt-in providers.** Unless `--providers` is given, the opt-in providers that meet the requirement are priced too.
  If one of them is recommended, a flag repeats its pricing caveat.
- **When nobody qualifies.** If no priced provider meets the requirement, an error flag says so. The cheapest
  provider is still shown, for comparison only.
- **The report.** It states the requirement and what the featured provider holds, and where. It also tells the
  reader what to ask for before signing, and the provider table gains a Certified column.

## What a certificate does and does not prove

- **Scope.** A certificate covers the legal entity, services and sites named in its scope statement, and nothing
  else. A data-centre operator's certificate (Equinix, Digital Realty, Iron Mountain) covers its building: power,
  cooling and physical access. It does not cover the bare-metal provider's servers, staff or processes. The
  catalogue records only certifications whose scope covers the provider's own bare-metal service.
- **Currency.** An ISO 27001 certificate lasts three years, with surveillance audits each year. A SOC 2 Type II
  report covers a past period, usually twelve months; ask for the latest one and a bridge letter for the time since.
  Reports are shared under NDA.
- **Evidence.** The catalogue's `evidence` field says whether it saw the certificate itself, the provider's own
  compliance page, or only an announcement. An announcement is enough to price a provider, not to sign with it.
- **Shared responsibility.** On bare metal the provider is responsible for the building, the hardware and the
  network to the server. The company runs everything above that: operating systems, access, patching, logging,
  backups and encryption. A hyperscaler ran part of that for them. Those controls move into the company's own
  ISO 27001 or SOC 2 scope, and its next audit will test them. The operations-effort line pays for running them,
  not for the audit work.

## Keeping the catalogue current

Each provider has `certifications_checked` (a date), `certifications` and `certifications_note`, a sentence shown
in the report.

- **What to record.** Only record a certification when the provider's own certificate, or its own compliance page,
  places its dedicated or bare-metal servers in scope. Use the certification body's register to confirm a
  certificate where you can.
- **What goes in the note.** If the scope is unclear, or the certificate covers only the cloud platform or only the
  buildings, record nothing and say why in `certifications_note`.
- **A wrong "yes" is worse than a missing one.** A client may sign on the strength of the certification.

Each entry looks like this:

```json
"iso27001": {"standard": "ISO/IEC 27001:2022", "issuer": "SOCOTEC Certification Deutschland GmbH (DAkkS)",
             "reference": "ZN-2025-35", "valid_until": "2028-09-26", "locations": "all",
             "evidence": "certificate", "scope": "The certificate's scope, quoted where possible.",
             "source": "https://..."}
```

- **`locations`.** Either `"all"` or a list of the provider's catalogue locations that the scope covers.
- **`evidence`.** One of `certificate`, `provider page` or `announcement`.
- **SOC entries.** For `soc2` and `soc1`, add `access`: how a customer gets the report.

Check certifications whenever prices are refreshed, and at once when a `valid_until` date has passed.
