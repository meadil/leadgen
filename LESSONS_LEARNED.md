# LESSONS LEARNED
The anti-repeating-mistakes file. Companion to `PLAN.md` (what to build)
and `PROJECT_STATUS.txt` (historical handoff).

Every entry here was learned the hard way — a live test, a wrong number,
or a silent failure. Each one is written so the next person (or the next
run of me) can recognise the situation and not walk into it again.

Format: **what happened → why → what to do instead.**

---

## A. FREE APIs AND SCRAPING

### L1. A browser User-Agent can get you BLOCKED where the default one works
**What happened:** Resolving `producthunt.com/r/XXXX` redirect links
returned 403 with a Cloudflare "Just a moment..." interstitial.
**Why:** I'd added a realistic Chrome User-Agent because the site looked
like it wanted one. Product Hunt is behind Cloudflare, which challenges
browser-looking UAs from datacenter IPs. The default `python-requests`
User-Agent passes straight through — 200 with the correct redirect.
**Do instead:** When resolving redirects, try the default User-Agent
FIRST. Only add a browser UA if you have proven it's required. Always
test a redirect resolver against one known-good URL before trusting it
in a pipeline. → fixed in `producthunt_adapter.py` (`self.web` session)
**Cost of not knowing:** This one bug was most of the missing email
addresses. Silent — the adapter returned `None` and the run "succeeded".

### L2. A redirect that "works" can still be worthless
**What happened:** `closeclerk.pages.dev` resolved fine, so it became
`company_domain`. It is not CloseClerk's domain — it's GitHub Pages.
**Why:** HTTP success ≠ useful data. A subdomain on a hosting platform
is where a site happens to live, not a company's identity. Emails
guessed onto these bounce.
**Do instead:** Maintain a list of hosting/platform/site-builder domains
and REJECT them as company domains — matching on suffix, not exact host,
so `foo.pages.dev` is caught too. When a platform subdomain is the only
thing you have, salvage the subdomain label as a *hint* and verify it by
DNS before using it. → `PLATFORM_HOSTS`, `GENERIC_SUBDOMAINS`, `_label_hint`

### L3. "Two runs of the same code gave different results" is a bug report, not noise
**What happened:** 95% domain coverage in one run, 9% in another, from
identical code.
**Why:** I nearly wrote it off as an API hiccup. It was a real bug:
`www.producthunt.com` and `producthunt.com` are the same host, and my
junk-host check tested the *raw* hostname, so the `www.` form skipped the
redirect and got rejected.
**Do instead:** When a metric swings wildly between runs, diff the
*inputs*, don't assume flakiness. And when matching "is this a junk
host?", always match on the NORMALISED form — strip `www.` first, or you
will handle both cases and still miss one.

### L4. Free tiers rate-limit silently and kill the middle of a run
**What happened:** PH returned 429 and GitHub returned 403 partway
through, and the adapters just stopped. The run printed "done" having
done a fraction of the work.
**Why:** A bare `raise_for_status()` + `break` turns a rate limit into
silent data loss. Nothing in the output distinguished "rate limited"
from "this query legitimately matched nobody".
**Do instead:** Exponential backoff with retries on 429/403, and — this
matters more — make the *skip* message name the actual reason. A run
that says "no matches" when it was actually throttled is worse than one
that crashes, because it looks like success. → `_graphql()` in the PH
adapter, retry loop in the GitHub adapter

---

## B. SEARCH QUERY SYNTAX (the expensive lesson)

### L5. AND-ing your own keywords silently returns zero, forever
**What happened:** `location:United States Founder Co-Founder type:user`
returned **0 results, for every country, forever.** The GitHub adapter
had been contributing nothing to the SaaS ICP and nobody noticed,
because "0 results" and "0 results because the query is nonsense" look
identical in the logs.
**Why:** GitHub ANDs space-separated terms across the whole profile. A
person can't be both "Founder" AND "Co-Founder" in their indexed text.
**Do instead:** OR your alternatives explicitly. And treat a `0` total on
a query you *expect* to match as a bug signal — verify the query against
the API by hand before shipping it. → `_build_query`

### L6. Parentheses are not universally supported
**What happened:** My first fix wrapped the ORs in parentheses for
readability. `(Founder OR CEO)` → **0 results.** Unparenthesised
`Founder OR CEO` → 1259.
**Do instead:** Read the target API's search syntax, and TEST each
construct against the live API. Never assume SQL/Ruby-style grouping
exists. Document the working form in a comment so the next person
doesn't "improve" it back.

### L7. Multi-word values in a qualifier usually need quoting
**What happened:** `location:United States` → **1** result.
`location:"United States"` → **1076.** All 8 countries in the ICP were
effectively dead.
**Do instead:** Quote any qualifier value containing a space. When a
location-based query returns suspiciously few results, test the quoted
form before concluding the location is rare.

### L8. Test the whole query, not the parts
**Do instead:** Before shipping any adapter, run its exact final query
string against the live API and check `total_count` is plausible. One
call. It would have caught L5, L6, and L7 immediately.

---

## C. DATA QUALITY

### L9. A free "company lookup" can be confidently wrong
**What happened:** Clearbit's autocomplete returned `[]` (nothing) for
three real small SaaS companies, and for "Brio AI" it returned
**an air purifier company**.
**Why:** Clearbit's free tier is weakest exactly where our niche lives
(small, new SaaS) and it always returns its best guess rather than
nothing. Silence and a confident wrong answer are very different risks —
only one of them is detectable downstream.
**Do instead:** Never trust a name→domain lookup unchecked. Require the
returned domain to *resemble the company name* before accepting it
(`domain_matches_company`). And demote name-lookups to LAST resort,
below anything the source told us directly. A real domain from a
redirect beats a guess from an API.

### L10. Containment is not identity
**What happened:** My first sanity check passed `brioairpurifier.com` as
a match for "Brio AI", because "brio" is a substring of it. The check I
wrote to PREVENTE L9 didn't prevent L9.
**Do instead:** The matched part must DOMINATE the domain, not merely
appear in it. Require a coverage ratio (e.g. the token must be ≥60% of
the domain label), and require similar length when comparing against a
concatenated company name. Test the guard with the exact wrong answers
you observed, not with invented ones.

### L11. Generic words are not signals
**What happened:** `"app"` was in the industry-synonym list, so
"a cute app for tracking your pet photos" scored **100% industry fit**
for a B2B SaaS ICP. The pipeline was confidently filling the CRM with
consumer apps.
**Why:** The word "app" appears in almost every product description on
earth. A keyword list only means something if each term actually implies
the thing you're detecting.
**Do instead:** For every synonym, ask "could this appear in a NON-match?"
If yes, drop it or require it alongside another signal. Match on whole
tokens, not substrings, so `app` doesn't fire inside `happy`.

### L12. Scoring a field you have no data for is not a judgement
**What happened:** PH exposes no location and no pain-signal copy, so
every PH lead silently lost 40% of the scoring weight and could never
exceed 60/100. The fix applied at the time was to lower the threshold
from 70 to 50 — which papered over a scoring bug and made the score
meaningless.
**Do instead:** Distinguish "this field doesn't match" from "the source
never gave me this field". Exclude unjudgeable fields from the average
and record what fraction of the weight was actually judged
(`_data_coverage`). If you find yourself tuning a threshold to
compensate for a structural scoring problem, the threshold is the wrong
knob.

---

## D. PROCESS AND VERIFICATION

### L13. An idempotency gate can be a badly-specified gate
**What happened:** I wrote the gate "run the pipeline twice, second run
must add 0 leads" and it failed. Then it became tempting to call that a
bug and "fix" it by making the source return stale results.
**Why:** The source legitimately returns NEW posts each day (the API is
ordered by newest). Zero-new-leads was the wrong specification — it
would have required deliberately breaking the source.
**Do instead:** Write gates that describe the property you actually
care about. Here: re-runs must add **no duplicates** and must not crash.
When a gate fails, first ask whether the gate is wrong. Changing a
*definition* to match reality is legitimate; quietly marking a test
passed is not. Record the rewrite and its reason (F18 in PLAN.md).

### L14. Clean up bad data before re-running, or you can't tell what worked
**What happened:** After fixing domain resolution, the DB still held 35
rows with placeholder `www.producthunt.com` domains, which made
before/after comparisons meaningless.
**Do instead:** When a data-producing fix lands, delete the rows the old
code produced so the new run re-adds them correctly. Otherwise the
metric improves for the wrong reason and the fix looks partial.

### L15. Verify each unit against the exact failure it's meant to prevent
**Do instead:** For a guard function, the test cases must include the
real wrong answers that occurred (`brioairpurifier.com` for "Brio AI"),
not only made-up ones. A guard tested only against invented inputs will
pass right past the actual problem.

### L16. Print what was skipped, and why
**Do instead:** The GitHub adapter originally "fixed" a rate limit by
catching the exception and continuing — which converted a loud failure
into a silent one. Every skip path should name its reason. If a run
reports "N leads written" it should also say what it dropped and why,
or the number is not trustworthy.

### L17. Don't trust a metric you can't recompute
**What happened:** The "leads are all duplicates" claim was investigated
by querying the DB directly, not by trusting the previous notes.
**Do instead:** Before acting on a claim about the data, query the data.
The claim was directionally right (duplicates + no pagination) but the
actual causes were different from what the notes implied, and two
distinct bugs (L1, L3) were hiding underneath it.

### L18. Before rejecting a whole source, ask what it is hiding
**What happened:** 41% of Show HN posts link to `github.com`, and the
junk-host filter threw every one of them away. Some were pure code dumps
— but the GitHub org API returns a company website and often a founder
email: `rsync-ai` → rsync.ai, `lemma-work` → lemma.work +
`deepak@lemma.work`. Ten good leads, discarded for free.
**Do instead:** A "junk host" is only junk as a *company domain*. Before
dropping a candidate because its URL looks unhelpful, check whether a
downstream API can turn that URL into something better. A repo link is
not a dead end, it's a lookup key.
**Also:** check whether the recovery path needs a credential. This one
silently returned nothing (HTTP 401) without `GITHUB_TOKEN`.

### L19. Measure WHY candidates are rejected, not just how many
**What happened:** HN was passing only 8% of candidates and it looked
like a filter problem. Instrumenting the rejection reason showed
**176 of 192 rejects were a single field** (`industry=0`) — not a broad
quality problem, one specific scoring failure.
**Do instead:** When a filter rejects a lot, bucket the rejections by
cause before changing anything. A single-field failure needs a scoring
fix, not a lower threshold.

### L20. Grade a weak signal; don't promote it to a pass
**What happened:** Functional words ("platform", "agent", "dashboard")
are a real signal that a product is business software, but no single one
proves it. Making that signal equal to an explicit category claim would
turn the filter into a rubber stamp.
**Do instead:** Return *partial* credit (0.6) for a weak signal, full
credit (1.0) for a strong one, and let the weights do the rest. The pass
rate went 8% → 35% with no loss of precision — the surviving leads were
manually checked and were real companies.

### L21. Rejecting duplicates within a single run is not "dedup"
**What happened:** A founder who Show HNs three products became three
CRM rows, because dedup was keyed on the post and each post is unique.
**Do instead:** Key dedup on the *person and the company*, not just the
source record. Any source where one person can produce many records
needs this. A second run can't fix it — the duplicates arrive in run #1.

### L22. "Real domain" ≠ "usable domain"
**What happened:** Only **52%** of resolved domains have MX records.
**Do instead:** Check MX before ever guessing an email, and treat
non-resolving domains as a distinct outcome — not as a domain, and not
as an error, just "no mail here". Report the two numbers separately;
conflating them makes a source look better than it is.

### L23. A source's "no data" is often a *different* field than you assume
**What happened:** Assumed HN would give names (it never does — F19) and
assumed the killer field would be geography or job title (it was
industry, F22).
**Do instead:** Read the actual API response shape before designing
around a field. Then instrument. Both assumptions were wrong and both
were cheap to check.

---

## F. NAMES, EMAILS, AND IDENTITY (from Chunk 3)

### L24. A non-empty name field is not a real name
**What happened:** dev.to lets any user set their display name, and many
set their handle: `audreybuildsai`, `BenedictVance6863`, `MACROGEN`,
`Product Watch`. These sailed through "is the name present?" and then
produced `spuriq.engineering@spuriq.ai` and
`posting.dude@postingdude.com` — addresses that look completely valid
and always bounce.
**Do instead:** Test the name, don't just check it for emptiness. A real
name has a space, no digits, no underscores, and no all-caps brand
words. And when you find one, **look at the generated output** — every
address you produce is worth eyeballing once.

### L25. An org account wearing a person's name is worse than a handle
**What happened:** "SpurIQ Engineering", "Posting Dude", "Product Watch"
look exactly like two-word names. They are companies. Guessing a
personal address from them is meaningless *and* wrong.
**Do instead:** Detect a company-shaped display name (ends in
Engineering / Labs / Studio / Software / Watch...), and when found, use
it as the company name and mark the record so enrichment skips email
guessing entirely.

### L26. Real names contain characters that are illegal in emails
**What happened:** "İlhan Neğiş" became `i̇lhan.neğiş@eringen.com` — a
combining dot above a capital İ, plus a non-ASCII ğ. It is not a valid
local part and it can never arrive.
**Do instead:** Before building any address, reduce the name to plain
ASCII alphanumerics. If too few usable characters remain, produce **no
guess at all** rather than a broken one. Transliterating is worse still
— it invents an address that silently fails.

### L27. If the "name" is the domain, it's not a name
**What happened:** A solo founder on a personal site gave
`hisashi.space@hisashi.space`.
**Do instead:** Before guessing, check the domain label isn't already
contained in the name. If it is, you don't have a person-and-company
pair, you have one piece of data used twice.

### L28. Print the generated data and actually read it
**Do instead:** Every garbage address in L24–L27 was found by reading a
printout, not by a test. A metrics-only test suite would have reported
"47% email coverage — great!" while the emails were unusable. Sample the
output with your own eyes at least once per new source.

### L29. A record with NO good key defeats every dedup check
**What happened:** 6 Product Hunt makers existed as 18 rows — the same
person inserted up to 6 times. They had no email (guess failed), no
GitHub URL, and no company domain (unresolvable), so `find_duplicate`
had nothing to match on. This is the same bug class as the old
"Hackerman" issue from months ago, and it recurred silently.
**Do instead:** For every dedup key you add, ask "what if this record has
*none* of them?" Always keep a last-resort key. Then **check for
duplicates after every run** — this took 3 chunks to notice because
nothing errored; 18 bad rows just sat there.
**Note:** a candidate from source X can also legitimately be a *different
person* from a candidate from source Y with the same company. Dedup must
merge on person+company, never on company alone.

### L30. Check whether you need a migration before writing one
**What happened:** Ready to add a generated column and index for
`source_url` dedup. Testing first showed PostgREST filters the JSON path
directly (`.filter('source_details->>source_url', 'eq', url)`) — no
schema change, no backfill, no index maintenance.
**Do instead:** Test the platform's existing capabilities before adding
schema. A migration you don't need is a migration you must maintain.

---

## G. KILLING A PLAN ON EVIDENCE (Chunk 4)

### L31. Reject a plan on measured evidence, not on vibes
**What happened:** Chunk 4 was "Wikidata precision layer", written before
testing it. Testing first showed the source was worthless for this niche:
founders with a company website came back as YouTube, the BBC, Intel and
Lufthansa; the 2–100 employee slice was a manga publisher and an NGO; a
query for *software-industry* founders returned **zero rows**; and
Wikidata contains **no company email addresses at all**.
**Do instead:** Any plan that proposes a data source should be spiked
against the real API *before* the adapter is written — one query, ten
minutes. If the answer is zero, that is a gift: you avoided building
something that would have produced uncontactable leads forever.
**Corollary:** a plan file is a hypothesis, not a contract. Rewriting a
chunk after testing is the system working, not the plan failing.

### L32. If the niche fit is wrong, find the API that already has the shape
**What happened:** Wikidata didn't fit, so we needed a company-level
source. GitHub *repository* search already had exactly the right shape:
every repo carries a `homepage` (the real company domain) and an `owner`
whose profile returns the company name, website and often a published
contact address. 704 repos → 433 usable domains → 101 real emails.
**Do instead:** When a source is the wrong shape, ask which API *does*
hold the field you need rather than forcing the wrong one. The cost of a
misshaped source isn't just a failed query — it's a CRM full of rows that
can never be emailed.

### L33. One API can return two different KINDS of entity — label them
**What happened:** A GitHub repo owner is either an `Organization` (name
is the company, contact is a role address like `hello@`) or a `User`
(name is often a real person). Both arrive in identical search results.
**Do instead:** Branch explicitly and record which kind it is. Saluting
`hello@frappe.io` as "Dear Frappe" is embarrassing; filing a real
developer as "a company" makes them unreachable and pollutes the CRM.

### L34. ALL-CAPS is how real people write their names
**What happened:** A rule rejecting all-caps names as "brand-like" threw
away real people: GitHub users routinely sign posts "SERGEY RUDENKO".
**Do instead:** Reject on *meaning* (a word list of company terms), not on
typography. Style is not a signal; vocabulary is.

### L35. A repo's `homepage` is often not the company's site
**What happened:** One founder's profile pointed at `codecanyon.net` —
Envato's marketplace, not their business. Docs hosts (readthedocs, gitbook)
are equally common.
**Do instead:** Treat a homepage field as a *claim*, and validate it
against a blocklist of documentation, package registries and marketplaces.
These lists will never be complete, so expect to find leaks by reading
output rather than by testing.

---

## H. EXTRACTING DATA FROM THE WEB (Chunk 5)

### L36. A regex over HTML will return filenames as email addresses
**What happened:** The first scraper reported a 40% hit rate. Nearly all
of it was junk: `Geist-Variable-l@in.woff`, `14659233656382_d@acenter.jpg`,
`modal-success-st@e.hidden`, `client.ch@.completions.cre`. Those are font,
image and JS filenames that happen to contain an `@`.
**Do instead:** Validate the TLD against an **allowlist**, not a pattern.
The true rate was 9% — the naive number would have made this look three
times more productive than it is. A wildly good result from a simple
extraction is a bug signal, not a win.

### L37. A page can contain other people's addresses
**What happened:** Company sites carry customer emails, testimonial
contacts and payment-provider addresses. Accepting those would send
outreach to strangers who look like perfect leads.
**Do instead:** Require the address to be on the company's own domain (or
be a personal mailbox). Cross-domain addresses are third parties, no
matter how real the mailbox is.

### L38. Obfuscation is the norm, not the exception
**What happened:** Sites write `hello [at] acme.com`, `hello(AT)acme(DOT)com`,
`hello &#64; acme.com`.
**Do instead:** Decode entity escapes and the common `[at]`/`(at)`/`[dot]`
patterns before extracting, or you will systematically miss the largest
companies — the ones most likely to publish an address.

### L39. Most small companies publish nothing
**What happened:** 85% of email-less leads have no address anywhere on
their site. They hide behind a contact form.
**Do instead:** Treat this as a measured ceiling and report it, rather than
looking for a scraping trick that isn't there. When a source has a real
ceiling, say so and redirect effort to the other gap (here: verification).

### L40. A schema field that is validated but never used is worse than absent
**What happened:** `company_size_employees_min/max` was required,
validated for min<=max, and then read by nothing. Changing 2 → 100 to
"make companies smaller" would have changed exactly zero behaviour, and
the run would have looked like it worked.
**Do instead:** Grep for every ICP field before assuming it is enforced.
An ICP is a wish until a scorer or filter reads it — and a test that
proves enforcement should exist (assert a too-large and a too-small
candidate score differently).

### L41. Record provenance from day one, and repair what predates it
**What happened:** 138 addresses existed with no record of how they were
found, because the status vocabulary was introduced after they were
written. We could not tell a company's real address from a guess.
**Do instead:** Every adapter that produces an address writes
`email_status` / `email_source` / `email_confidence` at the same time.
When the vocabulary changes, write a one-time repair — and expect to need
one, because it always arrives later than it should.

### L42. One hostile input must not abort a long run
**What happened:** A single domain raised a non-`requests` socket error
and killed a 200-lead backfill mid-way.
**Do instead:** Wrap the per-item body broadly, report the failure with
the domain that caused it, and continue. Long runs will always meet bad
input; the run should degrade, not stop.

---

## I. VERIFYING YOUR OWN DATA (Chunk 5.6)

### L43. A blocked vendor is not a blocked capability
**What happened:** Email verification was blocked for the entire project
because every third-party service requires a work email to sign up, and
the constraint is "$0, no paid APIs". The check those services charge
$16–80 per 1,000 for is: look up MX, then ask the mail server whether it
accepts this address. That is 40 lines of the Python standard library.
**Do instead:** When a plan is blocked on a *vendor*, check whether the
capability itself needs the vendor. Nine times in ten it does not. The
blocker was real; it just wasn't where we thought.

### L44. Measure the quality of your own data before you rely on it
**What happened:** After building the verifier, we measured what our
emails actually were: 27% of all stored addresses were dead, and the rate
split sharply by provenance — 7% bad for addresses a company published,
**51–62% bad for addresses we guessed**. Nobody knew. The list looked
fine at 53% coverage.
**Do instead:** Build the check, then run it across everything you have
and report the result by *how the data was produced*, not just overall.
The number that changed our behaviour was the per-source breakdown, not
the total. A single "53% coverage" figure had hidden a coin flip.

### L45. Only an explicit rejection is evidence of absence
**What happened:** SMTP returns many codes. 550 is "no such mailbox".
450 is "try again later". 252 is "I accept everything". 421/554 mean
*we* were refused — sometimes because our own IP is on a Spamhaus block
list, which says nothing about the address. A real, correct address was
rejected because of our infrastructure.
**Do instead:** Distinguish "proof of absence" (550/551/553/556, no MX)
from "absence of proof" (everything else). Only the first may drop a
lead. When a tool quietly discards leads because a server was slow or
unfriendly, it is worse than no tool — it destroys good data and reports
success.

### L46. A broad `except` can hide a 100% false negative
**What happened:** `dnspython`'s `Answer.sort()` raises `TypeError` on
this version. Our MX lookup sat inside `try/except Exception` that
reported "no MX". Result: **every single address looked like its domain
had no mail server** — a total, silent false negative that looked exactly
like a legitimate result.
**Do instead:** When a try/except collapses many failure modes into one
verdict, put the *reason* in the verdict. A catch-all handler that returns
a plausible answer is more dangerous than one that crashes, because it is
believed. Verify the failure path returns something visibly different
from a real result.

### L47. Library API changes break silently, not loudly
**What happened:** Two separate stdlib/library breakages: `smtplib.SMTP(sock)`
fails on Python 3.12+ (`'socket' object has no attribute 'find'`), and
`Answer.sort()` is broken above. Both surfaced only as `AttributeError`/
`TypeError` caught by our own handlers.
**Do instead:** When you catch a bare `Exception`, log what it was. A
first run of any network code should print the raw exception once, before
the handlers are trusted.

### L48. An IP-based block is not a verdict on the recipient
**What happened:** `srikanth.kasa@supero.dev` — a real address, found in
that company's own published contact — was refused with a Spamhaus SBL
notice because *our* IP is listed. Judging the address by that response
would have discarded a good lead for a technicality.
**Do instead:** Classify refusals as `blocked`, not `undeliverable`, and
keep the lead. Only a mailbox-level rejection is evidence about the
mailbox.

---

## J. DEDUP AND RESUMABLE CRAWLING (Chunk 6)

### L49. A similarity function may be case-sensitive, and you will not notice
**What happened:** `rapidfuzz.fuzz.ratio("SERGEY RUDENKO", "Sergey Rudenko")`
returns **21/100**, not 100. The dedup threshold was 90, so the same human
matched nothing and got inserted twice. GitHub users write names in all
caps constantly, so this was not an edge case — it was a whole class of
inputs failing silently while dedup reported success.
**Do instead:** Normalise before comparing — casefold, strip diacritics,
extract parenthetical aliases — and then compare. Test the matcher with
the *actual* casing variants your sources produce, not tidy sample data.

### L50. Bias a lossy matcher toward NOT matching
**What happened:** A >90 threshold plus `token_set_ratio` merged genuinely different people ("Beni Ganz" / "Beni Ganze" = 94.7). Tightened to 95 without token-set, which let a few real duplicates through instead.
**Do instead:** Ask which error is expensive. Merging two people loses a lead **permanently and silently**; missing a duplicate leaves an extra row that a stronger key or a cleanup pass will catch. Spend your error budget on the cheap mistake.

### L51. A checkpoint must store the identifier the API actually paginates by
**What happened:** The first paging checkpoint stored a page *number*. Product Hunt's GraphQL API paginates **only** by an `after:` cursor — there is no offset parameter. Resuming with `page=36, cursor=null` silently restarted at page 1, so the entire feature was a no-op that looked like it worked.
**Do instead:** Before writing a resumable crawler, find out what the API's continuation token actually is, and store *that*. Verify by fetching page 0, taking the returned cursor, fetching again, and asserting the two sets are **disjoint**. (PH cursors turned out to be base64 of the offset — `NQ`=5, `MTA`=10 — and consecutive pages have zero overlap.)
**Also:** a cursor you cannot persist is not a cursor. Have the generator write its position somewhere the caller can read it; a local variable the caller never sees is a bug waiting to happen.

### L52. A file opened without `encoding=` is a latent data-corruption bug
**What happened:** `icp_schema.load_icp` did `open(path, "r")`. On Windows that decodes with cp1252, so the em-dash in the ICP name became `â€”`. The corruption propagated into the database *and* into the paging-checkpoint key — meaning the checkpoint feature could never match the run that wrote it, and would have looked like a logic bug for hours.
**Do instead:** Always pass `encoding="utf-8"` to `open`, in every module, permanently. And keep non-ASCII out of **identifiers** entirely: an ICP id, a slug, or a cache key has no business containing an em-dash, and doing so guarantees that whoever changes the encoding later breaks something nobody will connect to this file.
**Tell:** if a value that came from a UTF-8 source displays as `â€"`, stop and look for a missing `encoding=`.

### L55. A resume key must match the write shape exactly — and prove it with overlap
**What happened:** Extending the paging checkpoint to Hacker News looked done: the attribute existed, `start_page` was read, the file was written. But the checkpoint is namespaced (`{"show_hn": {"page": n}}`, same as every other adapter) and the read was `self._resume["page"]` — which silently defaulted to **0**. Every run re-read page 1. A test asserting overlap == 0 caught it immediately (38/38 stories repeated).
**Do instead:** This is F52 repeating within the hour: read and write the same shape. Then assert the only thing that actually matters — **`overlap between consecutive runs == 0`**. "A checkpoint file exists" proves nothing; "the second run returned different posts" proves the feature works.
**Meta:** I had just written this exact lesson and immediately made the same class of error. Write the test before the second adapter, not after the third.

### L56. Verify each API's pagination style separately
**What happened:** Assumed Product Hunt's opaque base64 cursor was normal. It isn't — HN (Algolia), dev.to and GitHub repo search are all **page-based** with disjoint pages and no cursor at all.
**Do instead:** Probe each API's pagination before writing its checkpoint. Three of four needed a much simpler implementation than the first, and assuming the hard case made all three look uniform when they weren't.

### L53. A high call-count loop needs failure isolation
**What happened:** `find_duplicate` fires up to 5 DB queries per candidate — roughly 1000 round trips in a 200-candidate run. One dropped Wi-Fi connection raised out of the middle of it and destroyed the entire run, losing every lead not yet written.
**Do instead:** Wrap each check, fail **open** (a failed dedup check means "possibly a duplicate", not "abort"), and make the summary line report failed writes explicitly. A run that quietly lost 80 leads because it printed a success-looking summary is the worst outcome available.
**Corollary:** a multi-minute operation that only tests on a perfect network is not tested.

---

## E. THE STANDING CHECKLIST (before shipping any adapter change)

1. Run the **exact final query string** against the live API. Is
   `total_count` plausible?
2. Test the redirect/domain resolver against **one known-good URL**
   before trusting it in a run.
3. Would a **wrong** result be detectable? (guards, sanity checks)
4. Does every synonym/skip path survive "could this be a non-match?"
5. Re-run the pipeline **twice** — are duplicates actually suppressed?
6. Do the numbers in the DB match the claim? Query, don't assume.
7. Before rejecting a candidate on a "junk host", check whether a
   downstream API can turn that URL into a real company (L18).
8. Bucket every rejection by cause before tuning a filter (L19).
9. Grade a weak signal as partial credit — never promote it to a pass (L20).
10. Dedupe on person + company, not just on the source record (L21).
11. Report "has domain" and "domain accepts mail" as two numbers (L22).
12. Test every generated email by eye — coverage % hides garbage (L28).
13. For every dedup key, ask what happens when a record has none (L29).
14. Check duplicate groups in the DB after every run, not just failures.
15. Test platform capabilities before writing a migration (L30).
16. Spike a proposed data source against the real API before building it (L31).
17. When a source is the wrong shape, find the API that has the right one (L32).
18. One API can return different KINDS of entity — branch and label them (L33).
19. Reject on meaning, not typography (L34).
20. Treat every "website" field as a claim; validate it (L35).
21. Validate extracted emails against a TLD allowlist (L36).
22. Never accept an address on a domain other than the company's (L37).
23. A suspiciously great extraction result is a bug signal (L36).
24. Grep that every ICP field is actually read by something (L40).
25. Write provenance at the moment an address is created (L41).
26. Wrap long runs per-item so one bad input cannot abort them (L42).
27. When a vendor blocks you, check whether the capability needs them (L43).
28. Report data quality BY PROVENANCE, not just as an overall total (L44).
29. Only an explicit rejection justifies dropping a record (L45).
30. A catch-all handler returning a plausible verdict is worse than a crash (L46).
31. Never treat an IP-level refusal as a verdict on the recipient (L48).
32. Normalise before fuzzy-matching, and test with real casing variants (L49).
33. Spend a lossy matcher's error budget on the cheap mistake (L50).
34. Store the continuation token the API actually uses; prove pages are disjoint (L51).
35. Always pass encoding="utf-8" to open(); keep identifiers ASCII (L52).
36. Isolate failures in any loop that makes many network calls (L53).
37. Prove a resume feature with overlap==0, not with "the file exists" (L55).
38. Verify each API's pagination style separately (L56).

---

*Started 2026-09-29, during Chunk 1. Entries added as chunks complete.*
