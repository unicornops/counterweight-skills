# Writing up results in Counterweight's voice

The report script handles layout and most wording. This is for everything you write around it: the chat summary, an email, a follow-up answer.

## Voice

- **We** is Counterweight; **you** is the client. Engineer to CFO: precise enough for the platform team, plain enough for finance.
- **Numbers before adjectives.** "€31k a month today; €16k–€18k on bare metal" rather than "dramatic savings". Never round to make a number look bigger.
- Every figure carries a currency and a period: `€14,500/mo`, `€174k/yr`, `€0.09/GB`. Euro sign before the number, no space. `k` and `M` in headlines; full figures with commas in tables.
- Percentages are whole numbers unless the decimal matters.
- Dates as `12 January 2027`.
- Offer names are proper nouns: **Teardown**, **Exit Plan**, **Move**, **Steady State**.
- Calls to action are a verb plus the offer: "Book a Teardown", "Price your exit". Never "Learn more" or "Get started".
- Words to use: exit, leverage (noun only), move, workload, bill, renewal, egress, tested, costed, bare metal, repatriate.
- Words never to use: journey, unlock, transform, seamless, empower, cutting-edge, synergy, "leverage" as a verb, "cloud-native" as praise. No emoji, no exclamation marks.

## Shape of the chat summary

Keep it short; the report has the detail.

1. One sentence with the three numbers: bill today, bare-metal range, saving range (say it includes operations effort).
2. What drives the saving: two or three specifics with figures from the plan ("€4.0k/mo of egress is included in the server price", "€3.1k/mo of support plan replaced by the operations line").
3. What stays and why, in one sentence.
4. Confidence and the single thing that would sharpen it.
5. The next step: a Teardown, with hello@counterweight.ie.

Example:

> Your September bill averages €30.8k/mo. Moving the parts that suit bare metal to Hetzner comes to €17.8k–€19.2k/mo, a saving of €11.6k–€13k/mo (38–42%) after allowing €5.1k–€5.3k/mo for the extra operations work. Most of it comes from egress (€4.0k/mo, included in Hetzner's server prices), the €3.1k/mo support plan, and compute and managed databases consolidated onto 8–9 servers. CloudFront, Glacier and DynamoDB stay as they are (€3.4k/mo). Confidence is medium-high: 90% of the sized spend came from usage data; measured CPU utilisation would narrow the range. A Teardown, run by your team on read-only exports and reviewed by Counterweight, would firm this up in two weeks: hello@counterweight.ie.

## Things never to say

- That the estimate is a quote, a guarantee, or what Counterweight will charge.
- A saving without its range, or without saying whether operations effort is included.
- That a provider's price is current if the catalogue is old; give the date.
- Anything that sounds like legal or financial advice. Regulatory points are context ("the Data Act bans switching charges from 12 January 2027"), not instructions.

## When there's little or no saving

Say so first, then why (small estate where minimum server counts and operations effort dominate; mostly proprietary managed services; mostly archive storage or spot capacity), then what would change it (a larger footprint, a phase-two move of data services, renegotiating the cloud contract with a costed exit as leverage). A costed exit is useful at renewal even when the answer is "stay".
