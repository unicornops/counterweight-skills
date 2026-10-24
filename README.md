# Counterweight skills

[Agent Skills](https://agentskills.io) from [Counterweight](https://counterweight.ie), the cloud cost and exit
engineering practice. They run in your own AI assistant, on files you give it.

| Skill | What it does |
|---|---|
| [`counterweight-bare-metal-estimate`](plugins/counterweight/skills/counterweight-bare-metal-estimate/SKILL.md) | **Price your exit.** Turns an AWS, Azure, Google Cloud, Oracle Cloud, IBM Cloud or Alibaba Cloud bill into an indicative bare-metal plan: named Hetzner, OVHcloud and Scaleway servers (Leaseweb, Cherry Servers, Latitude.sh, phoenixNAP, Teraswitch and Vultr on request), monthly euros, the saving with and without operations effort, one-off costs, and what should stay in the cloud. Writes a report in Markdown and branded HTML. [Sample report](https://counterweight.ie/sample-estimate). |

## Install

**Claude Code**: add this repository as a plugin marketplace and install the `counterweight` plugin, which holds
every skill here:

```
/plugin marketplace add unicornops/counterweight-skills
/plugin install counterweight@counterweight-skills
```

**Claude apps (claude.ai, desktop)**: download a skill's zip from the
[latest release](https://github.com/unicornops/counterweight-skills/releases/latest) or from
[counterweight.ie](https://counterweight.ie/price-your-exit), then upload it under your skill settings
(Customize › Skills › Upload a skill). Code execution must be on.

**Other assistants**: any tool that reads Agent Skills and can run Python 3.9 or newer can use a skill folder as it
is. Copy `plugins/counterweight/skills/<skill>` to wherever your tool loads skills from.

Then give the assistant your bill export and ask what it would cost on bare metal.

## Your data

The skills work only on files you give your assistant. They never ask for cloud credentials, and their scripts make
no network requests. When your assistant has web access, the bare-metal estimate may refresh its price list from the
providers' public price pages, if the list is more than about 60 days old or you ask for current prices; nothing
from your bill goes into those requests. Counterweight receives nothing unless you send it to us. Your assistant's
provider handles your files under your agreement with them.

## Estimates, not quotes

Results are indicative estimates from billing data and dated provider list prices. They are not a quote, an offer or
what Counterweight would charge, and they are not legal or financial advice. A
[Teardown](https://counterweight.ie/teardown) replaces the assumptions with measurements.

## Developing

```
python3 -B -m unittest discover -s tests -v   # spec checks and each skill's demo, end to end
python3 -B scripts/package.py                  # dist/<skill>-<version>.zip, one folder with SKILL.md on top
claude plugin validate ./ && claude plugin validate ./plugins/counterweight
```

- Each skill follows the [Agent Skills specification](https://agentskills.io/specification). Its frontmatter may
  use only `name`, `description`, `license`, `compatibility`, `metadata` and `allowed-tools`, because the Claude apps
  reject anything else on upload.
- Releases are automated with [release-please](https://github.com/googleapis/release-please). Use
  [Conventional Commits](https://www.conventionalcommits.org) for PR titles (PRs are squash-merged): `fix:` makes a
  patch release, `feat:` a minor one, and `feat!:` or a `BREAKING CHANGE:` footer a major one. Each merge to `main`
  updates a release PR; merging that PR tags `vX.Y.Z`, writes the version into `plugin.json` and every skill's
  `metadata.version`, updates `CHANGELOG.md`, and attaches a zip per skill to the GitHub release. Don't edit versions
  by hand. A new skill's `SKILL.md` goes in `extra-files` in `release-please-config.json`, with
  `version: "X.Y.Z" # x-release-please-version` in its metadata (a test checks both).
- The bare-metal estimate's prices are dated (`assets/catalogue.json`); refresh them roughly every 60 days following
  its `references/refreshing-prices.md`.

Issues and pull requests are welcome. For anything you would rather not post in public, email
[hello@counterweight.ie](mailto:hello@counterweight.ie).

## Licence

Apache License 2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)). The Counterweight name and logo are trademarks of
Unicorn Operations Limited and are not licensed. Bundled fonts (Archivo, IBM Plex Mono) are under the SIL Open Font
License 1.1, with their licences alongside them.

Counterweight is a trading name of Unicorn Operations Limited, registered in Ireland, company number 774973.
