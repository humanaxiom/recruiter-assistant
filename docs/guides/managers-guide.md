# recruiter-assistant — the manager's guide

**For:** the Director of Talent/Technology Operations and the pilot recruiters
and hiring managers using **https://sfuai.ca:8000**.

**What this is.** A step-by-step description of what the product actually does
today, screen by screen, derived from the code that is deployed — not from a
plan. Where the product does **not** do something you would reasonably expect,
it says so in a **Be aware** box rather than leaving you to discover it.

Every statement here is traceable to a file in the repository; the mapping is in
the appendix at the end, which you can ignore.

**Two conventions used throughout:**

- *Writer* means an **admin** or a **recruiter**. Those two roles can change
  things. Hiring managers and auditors can only look.
- Timings are the measured ones from the pilot box. Where no number has been
  measured, this guide says "minutes" rather than inventing one.

---

## 0. Before your first session — a checklist for the DTO

Do these before anyone sits down with the tool. Each one blocks real work if it
is missing.

1. **Get everyone signed in once, then assign roles.** A first CAS login creates
   an account with **no role**, which can do nothing at all. An admin must then
   grant each person a role on the Users screen. Plan for this: the first login
   is a dead end by design, and it looks like a fault if nobody warned the user.
2. **Have the real JD file, not the summary blurb.** The ranking is only as good
   as the requirements the parse can find. A two-paragraph careers-page summary
   produces a thin requirement set, and a JD that yields *no* skills at all is
   refused for ranking outright. Bring the `.pdf`, `.docx`, `.txt` or `.json`
   posting.
3. **Check the JD file's encoding if it came from Windows.** At least one real
   pilot JD arrived as Windows-1252 and pushed garbled characters into every
   screen that rendered it. If the text looks wrong after upload, it was the
   file.
4. **Export the Taleo "All Candidates" roster** for the requisition — but do not
   upload it until the résumés have finished parsing (§5). It needs `Name` or
   `Email` columns; `Work Authorization`, `APSA Internal`, `CUPE Internal`,
   `SFU ID` and `Submission Date` are used when present.
5. **Budget the parsing time.** Roughly **29 résumés per hour** (§4). 75 résumés
   is about 2.5 hours; a full 315-candidate requisition is about **11 hours**.
   Start the upload well before the meeting where you want the shortlist.
6. **Use the full URL with the port: `https://sfuai.ca:8000`.** There is no
   redirect from `http://` and no plain `sfuai.ca` — a bookmark without the
   `:8000` will not reach the product.

---

## 1. Signing in, and what each role can do

### What you see

Go to **https://sfuai.ca:8000**. You are redirected straight to SFU CAS. Sign in
with your SFU credentials.

After CAS returns you, one of three things happens:

| Situation | What you see |
|---|---|
| You have a role | The **Jobs** list |
| You are signed in but have **no role yet** | A page headed **"Pending access"** — "You are signed in, but your account has no role yet. Please contact an administrator." |
| The API behind the site is down | **"Backend unavailable"** |

The header across the top carries: **recruiter-assistant** (home), **Jobs**,
**Users** (admins only), **Access record** (admins and auditors only), your
username with your role in brackets, and **Logout**.

### The four roles

| Role | Can do |
|---|---|
| **admin** | Everything a recruiter can do, **plus** the Users screen (assigning roles) and the Access record. |
| **recruiter** | Create and edit requisitions, upload JDs and résumés, import the roster, record work-authorization declarations, generate shortlists, reveal identities, withdraw and reinstate candidates, download documents, export. |
| **hiring_manager** | **Read-only.** Sees only the requisitions assigned to them, not the full job list. Cannot upload, cannot generate a shortlist, cannot reveal an identity, cannot record a declaration. |
| **auditor** | **Read-only**, but unscoped — sees everything, plus the **Access record**. Every single-record read an auditor performs is itself written into the access record. |

Hiring managers and auditors do not merely get an error if they try a writer
action — the controls are not rendered for them at all.

### How an admin grants a role

1. Header → **Users**. (Only visible to an admin; anyone else gets a 403.)
2. The table lists every account: username, display name, email, current role
   (or "no role"), and whether it is active.
3. In the **Assign role** column pick `admin`, `recruiter`, `hiring_manager` or
   `auditor` from the dropdown and press **Save**.
4. The change takes effect immediately and is written to the access record as a
   `role_changed` event.

**How long:** instant.

**What can go wrong:** demoting the **last remaining admin** is refused with a
message on the page — the system will not let you lock the organisation out of
its own administration.

> ### ⚠️ Be aware
>
> - **A first login is a dead end until an admin acts.** This is deliberate
>   (fail-closed), but the "Pending access" page gives the user no way to
>   request access and sends no notification to an admin. Someone has to tell
>   the admin out of band.
> - **Any SFU CAS user can authenticate**, which creates a no-role account row.
>   They can do nothing, but the account list will grow with people who were
>   just curious.
> - **There is no screen for assigning a requisition to a hiring manager.** The
>   capability exists in the API, but no page in the product calls it — so a
>   hiring manager signing in today will see an **empty job list**, because
>   "their" requisitions cannot be assigned through the interface. Until that is
>   built, hiring managers should be given the `recruiter` role or shown
>   shortlists by a recruiter.
> - **Rotating the application's session key logs everyone out at once.** If
>   that is ever done for security reasons, warn the four users first or it
>   reads as a fault.

---

## 2. Creating a requisition

### What you see

The **Jobs** list has a filter row (All / draft / open / closed / archived) and a
table: Title, Department, Location, Source, Status, Updated, Résumés. Above it
are two collapsed panels — **+ New job** and **+ Bulk upload job descriptions**
— and, for admins only, an **Import jobs from Taleo** button.

### Option A — one job, typed or pasted

1. Click **+ New job**.
2. **Title** — required, 2 to 200 characters.
3. **Department** — free text, up to 100 characters.
4. **Campus** — a type-ahead offering Burnaby (BBY), Vancouver (YVR), Surrey
   (SRY). Codes are accepted and normalised (`bby` becomes `Burnaby`); anything
   else is kept exactly as typed. Left blank, the JD parse fills it in *if* the
   posting states a campus.
5. **Minimum years of experience** — optional, 0–50. This is what the
   *experience* sub-score is measured against; leave it blank and every
   candidate scores full marks on that row (and the shortlist says so).
6. **Shortlist size (% of ranked candidates)** — 1 to 100, default **100**
   (keep everyone who was ranked).
7. **Prefill from a JD file** — choose a `.pdf`, `.docx`, `.txt` or `.json` (max
   10 MB). The text is extracted and dropped into the Description box for you.
   It does **not** create the job; you still press Create.
8. **Description** — required, at least 50 characters, up to 200,000. This is
   the JD of record.
9. **Additional requirements (optional)** — see below.
10. **Blind review** — a checkbox, **unticked by default**.
11. **Create job.** You land on the new job's page, in **draft** status, with the
    JD parse already running.

**How long:** the form submit is instant. The JD parse behind it takes up to a
couple of minutes (§3).

### The "Additional requirements" box — what it is for

This is the hiring manager's own list of what they want, whether or not the
posting mentions it. It is deliberately kept **separate** from the Description,
so the shortlist can show which requirements came from the posting and which
came from you.

- Anything you state plainly is treated as **required**. Write "nice to have",
  "bonus" or "ideally" to soften it.
- It is worth **10% of the final score** (the `manager_prompt` weight). The
  posting's own requirements and evidence carry the other 90%.
- It is **editable later** from the job page (see §3), and editing it re-reads
  the note immediately — but it does **not** re-rank anyone. Use **Regenerate**
  on the shortlist to apply it.
- It is **not a substitute for a JD.** If the JD parse yields no requirements at
  all, the product refuses to rank the job even when this box is full — ranking
  on the manager's note alone is exactly the failure this guard exists to
  prevent.

### Option B — many jobs at once (bulk upload)

1. Click **+ Bulk upload job descriptions**.
2. **JD files (or a .zip)** — up to **50 files** per upload. Accepted:
   `.pdf`, `.docx`, `.txt`, `.json`, or one `.zip` containing those.
3. **Metadata manifest (optional CSV)** — a sidecar with a required `filename`
   column and any of `title`, `department`, `location`, `employment_type`,
   `seniority`, `min_years`, `retention_days`, `blind_review`. The filename is
   matched on its base name, ignoring folders and case.
4. **Upload job descriptions.** You get a results table: one row per file, with
   **created** / **duplicate** / **failed** and a link into each created job.

One **draft** job is created per file. Without a manifest, the job's title is
derived from the filename, and the job page tells you so — the JD parse replaces
it with the title stated in the posting.

### Option C — Taleo import (exists, and must stay off)

Admins see an **Import jobs from Taleo** button on the Jobs list. **It is
switched off for this deployment and must stay off**: enabling it lets the box
make an outbound internet request, which requires an enumerated firewall rule,
counsel and privacy-officer sign-off, and a named owner — none of which have
been done. Pressing it today queues a run that records "skipped" and makes no
request.

### Department and campus after the fact

On any job page, writers get an **Edit department & campus** panel (open
automatically when either is missing). Whatever you type wins permanently: a
re-parse fills those fields only when they are empty and never overwrites a
value you entered.

> ### ⚠️ Be aware
>
> - **Department is free text and will not group.** Measured on real SFU JDs,
>   one unit came back with four different spellings, three differing only in the
>   apostrophe. Do not expect to filter or report by department.
> - **Retention period, employment type and seniority are not on the create
>   form.** They exist in the data and can be set through the bulk CSV manifest,
>   but there is no screen for them.
> - **Shortlist size (%) can only be set when the job is created.** There is no
>   screen to change it afterwards.
> - **The Description cannot be edited after creation.** The only editable
>   fields on an existing job are department, campus, additional requirements and
>   the blind-review switch. If the wrong text was uploaded, create a new job.
> - **A duplicate in a bulk upload is detected on the JD text**, so re-uploading
>   the same posting reports "duplicate" rather than creating a second job.

---

## 3. The JD parse

### What you see

A new job opens in **draft** with a **parsing…** badge. The page polls itself
every 3 seconds; you do not need to refresh.

When the parse lands, the **Parsed requirements** section fills with **skill
pills** — one per required skill the model found in the posting — and the badge
disappears. The job's title, department and campus may also be filled in from
the posting if they were blank.

If you entered additional requirements, a **Your additional requirements** block
appears below, showing your text verbatim and, underneath, how it was scored:
chips marked `· required` for plain statements, unmarked chips for softened
ones, and a "Not scored as skills (shown to reviewers only)" line for anything
that is not a skill (travel, hours, and so on).

**How long:** typically under two minutes for one JD. A bulk upload of 20+ JDs
queues them and takes correspondingly longer.

### Three outcomes

| Outcome | What you see | What to do |
|---|---|---|
| **Parsed** | Skill pills | Continue to "Open for applicants" |
| **Parse failed** | A red **Parse failed: &lt;reason&gt;** line, and a **Re-parse JD** button | Press **Re-parse JD** |
| **Parsed, but nothing found** | Skill pills area says "No required skills parsed", plus an orange warning: *"This job description yielded no requirements at all… wrong text uploaded? Re-parse or replace the JD."* | See the Be aware box below |

The **Re-parse JD** button only appears when the parse actually *failed*, and it
only works while the job is still in **draft**. This is deliberate — offering a
retry during a healthy in-flight parse would queue the same work twice.

### Opening the job

Résumés cannot be uploaded to a **draft** job. Under **Status** you get the
legal next steps:

- draft → **Open for applicants** or **Archive**
- open → **Close** or **Archive**
- closed → **Archive**

**Open for applicants** is greyed out until the JD has parsed, with a tooltip
saying why ("Wait for the LLM to finish parsing the JD", or, if it died,
"Parsing failed — re-parse the JD before opening for applicants"). Once you
press it, the **Upload résumés** section appears.

### Blind review

The job page shows **Blind review: on/off** with a single toggle button. It is
**off by default**. Turned on, candidate identity is hidden everywhere on the
review surfaces — shortlist cards show "Candidate A", "Candidate B" and so on,
and seeing a real name requires an audited **Reveal identity** click. Toggling
the switch is recorded in the access record.

> ### ⚠️ Be aware
>
> - **A JD that parses to zero requirements has no recovery path in the
>   interface.** The warning says "Re-parse or replace the JD", but the Re-parse
>   button only renders when the parse *failed*, and there is no screen to
>   replace the description text. In practice: **create the requisition again
>   with better JD text.** This is the single most likely thing to strand you.
> - **A thin JD produces a thin ranking.** The requirement set the model
>   extracts is all the shortlist has to compare against. A careers-page summary
>   is not the same document as the full posting.
> - **Editing "Additional requirements" costs an AI call and does not re-rank.**
>   The page says so. Use **Regenerate** on the shortlist afterwards.
> - **Nothing alerts you to a dead JD.** In an earlier incident 20 JDs sat dead
>   behind a spinner for 24 hours. The display is fixed — a failed parse now
>   looks different from a running one — but no automated check reports them, so
>   after a bulk upload, look at the list.

---

## 4. Uploading résumés

### What you see

On an **open** job, writers get an **Upload résumés** panel with five controls:

1. **Résumé file(s)** — one, many, or a single `.zip`.
2. **Cover letter file (optional)** — one file, applied to the whole batch.
3. **Pairing manifest (manifest.json)** — optional, for per-résumé pairing.
4. **…or paste cover letter text (optional)** — a free-text box.
5. **I confirm the candidate consented to this processing (PIPEDA/FIPPA)** —
   **required**. Nothing is sent until this is ticked.

Then **Upload**.

### Limits

| | Limit |
|---|---|
| Files per upload | **50** (a `.zip` counts its entries against the same cap) |
| File size | **10 MB** each |
| Accepted résumé types | `.pdf`, `.docx`, `.rtf`, `.txt` |
| Zip total | 100 MB uncompressed, 10 MB per entry, 50 entries |
| Pairing manifest | JSON, up to 1 MB |

### Pairing cover letters by filename

If you upload résumés and cover letters together, the product pairs them by
name. The rules, exactly:

- A file whose name ends in **`resume`** or **`cv`** is a résumé.
- A file whose name ends in **`cover letter`**, **`coverletter`**,
  **`cover note`** or **`cover`** is a cover letter.
- The suffix must be separated from the rest of the name by a **space, dash or
  underscore** — they are interchangeable, and case does not matter. So
  `A Smith_Resume.pdf` pairs with `A Smith Cover Letter.pdf`.
- A file with **no recognised suffix is treated as a résumé**.
- A cover-letter-named file with **no matching résumé is ingested as a résumé
  anyway**, with a warning on the results summary — a stray name never loses a
  document.

### Pairing by manifest

Upload a `manifest.json` in its own field:

```json
{"applicants": [
  {"resume_file": "0001_resume.pdf",
   "cover_letter_file": "0001_cover_letter.pdf",
   "cover_letter_flag": "Yes"}
]}
```

`cover_letter_flag` is a veto: anything other than `true/1/yes/y/t` means "no
cover letter for this applicant" even if a file is named. A manifest entry
naming a résumé that was not uploaded is reported as a **rejected** row, never
silently dropped.

> **The manifest must be its own upload field, not inside the résumé zip.** A
> `manifest.json` zipped with the résumés is rejected, with a message telling you
> exactly that.

### What happens next

You return to the job page with a summary line at the top, for example:

> `12 accepted (5 with a cover letter), 2 duplicate, 1 rejected`
> `Rejected somefile.xyz: unsupported file type`

Then:

- **Résumé status** — a live five-bucket count: Uploaded, Parsing, Parsed
  (with a "(N degraded)" note), Failed, Withdrawn.
- **Résumés** — a table of Candidate / File / Status / Uploaded, refreshing
  every 3 seconds until every row is finished.

A row can carry extra badges: **withdrawn** (excluded from shortlists, kept for
the record) and **degraded** (the AI skills extraction failed and fell back to a
keyword scan — the résumé is **excluded from shortlists** until re-parsed).

### How long this takes — read this before planning a session

Parsing runs on the local GPU at roughly **29 résumés per hour** (measured:
batches of four, about 8 minutes per batch). Each résumé is two or three
sequential AI calls.

| Résumés | Expect |
|---|---|
| 10 | ~20 minutes |
| 75 | ~2.5 hours |
| 315 | ~**11 hours** |

The upload form's own hint ("~1–2 minutes each") is per file at low volume; at
batch scale the table above is what actually happens. **This is a hardware
ceiling, not a setting.**

> ### ⚠️ Be aware
>
> - **A duplicate is detected by exact file content within the same job.** The
>   same person's résumé re-saved or re-exported is a *new* résumé, not a
>   duplicate.
> - **A "degraded" résumé silently never joins the ranking.** It shows as
>   `parsed` in the status column with a small badge. The shortlist page does now
>   tell you how many there are, but the only fix is to **upload the file again**
>   — there is no re-parse button for a résumé.
> - **Per-résumé pairing and a single batch cover letter cannot be combined.**
>   Doing both is rejected rather than guessed at.
> - **There is no notification when parsing finishes.** No email, no in-app
>   badge. You have to come back and look.

---

## 5. Importing the candidate roster (Taleo CSV)

### When to do it — the ordering matters

**Upload résumés → wait for parsing → import the roster → generate the
shortlist.**

The roster is matched to your résumés by **email** first and then by **name**,
and both of those come out of the résumé *parse*. Import the roster before
parsing finishes and it will match **zero rows**, with nothing on screen
explaining why.

### What you see

On any job page, writers get **Import candidate roster (Taleo)** with a single
file input for the **"All Candidates" export (CSV)**, then **Import roster**.

The CSV needs at least a `Name` or an `Email` column. It also reads, when
present: `Work Authorization`, `APSA Internal`, `CUPE Internal`, `SFU ID`,
`Submission Date`. Column names are matched loosely (case, spaces and
underscores are ignored).

### What it does

For each row it can confidently match to one of this job's résumés, it records:

- **Work authorization.** The Taleo prescreen answer, mapped as: *No
  restrictions* → eligible; *Work Permit* → **eligible** (an explicit sponsor
  decision); *Study Permit* → not eligible; *Not eligible to work in Canada* →
  not eligible. **Any value the tool does not recognise becomes "unknown", never
  "not eligible."** A blank cell changes nothing.
- **SFU internal status** from the `APSA Internal` / `CUPE Internal` columns. An
  internal candidate receives a **+0.05 (5 point) uplift** on their final score,
  disclosed on their shortlist card. Being both APSA and CUPE does not double
  it. A roster that does not carry those columns at all leaves existing flags
  untouched.

**How long:** seconds. This is a plain CSV read with no AI in it.

### The report

You return to the job page with a summary:

> `39 matched (12 work-authorization change(s), 3 APSA change(s), 1 CUPE change(s))`
> `240 roster row(s) could not be matched to a résumé.`
> `Any existing shortlist for this job does not reflect this import until it is regenerated.`

**Read those two numbers in the right direction.** The Taleo export is the
**whole** candidate pool for the requisition; the résumés you uploaded are a
**subset**. A large "could not be matched" number is the **normal, permanent
state** — it counts roster rows with no résumé here, not failures. The number
that matters is how many of *your résumés* were covered, which you get from
`matched` against your parsed count. On the pilot box that was 39 of 39 —
100%.

### When it refuses rather than guesses

The import deliberately leaves things unwritten rather than picking:

- A roster row whose **email matches two or more résumés** on this job.
- A roster row whose **name matches two or more résumés**, or one résumé matched
  by two or more roster rows.
- **The same person appearing twice in the CSV with contradicting answers** for a
  field — that field is refused for that candidate; other fields still apply.

These are reported and left alone. A wrong attribution is worse than an absent
one, because it is invisible.

> ### ⚠️ Be aware
>
> - **Importing a roster overwrites a recruiter's manual declaration.** If you
>   corrected someone's work authorization by hand and then import (or re-import)
>   a roster that names them with a recognised value, the CSV wins. A *blank* or
>   unrecognised cell does not overwrite. Order of operations: **roster first,
>   hand corrections after.**
> - **An existing shortlist does not reflect the import.** The uplift is applied
>   when a shortlist is generated. You must press **Regenerate**. (Work
>   authorization is the exception — that is re-read on every page load.)
> - **The report is shown once, as a flash message.** It is not stored anywhere
>   you can go back and read. Screenshot it if it matters.
> - **The roster CSV itself is never saved.** It is read in memory, matched, and
>   discarded. Only the three screening columns survive, plus counts and line
>   numbers in the access record. This is a privacy strength, but it also means
>   there is no record of "what the CSV said" to go back to.
> - **The "unknown" branch has never been exercised by real data.** All 315 rows
>   in the sponsor's real export carried a recognised answer. A clean import is
>   not proof that an unrecognised value behaves correctly.

---

## 6. Work authorization

### Where

Two places, both for writers only:

1. **The candidate page** (§8) — three radio buttons plus an optional **Note**.
2. **Each shortlist card** — the same three radio buttons, no note. This is the
   fast path, because it is where you are actually comparing people.

### The three states

| Option | Meaning |
|---|---|
| **Not recorded** (default, pre-selected) | Nobody has declared anything. Carries **no adverse meaning**. |
| **Eligible** | The candidate declared they can work in Canada. |
| **Not eligible** | The candidate declared they cannot. |

"Not recorded" is listed first and pre-selected on purpose — a form that
defaulted to or visually favoured "not eligible" would nudge reviewers toward an
adverse finding on a protected ground.

**The system never infers this from a résumé.** It comes from the candidate's own
Taleo answer or from your explicit declaration, and nothing else.

### What "Not eligible" does to a card

The candidate **stays visible**, and:

- The rank becomes **—** (no rank is assigned — a rank is itself a merit claim).
- Every sub-score and the headline score read **n/a**.
- A banner on the card explains: *"Ranked last: no Canadian work authorization…
  the scores below are not applied and no rank is assigned. Nothing has been
  deleted — the full evidence is still on the candidate page. If this is wrong,
  correct the declaration there."*
- The declaration control still renders **on that card**, so you can undo it in
  one click.

Nothing is deleted and this is reversible at any time.

**How long:** instant. The band is applied when the page is read, not when the
shortlist is generated — so a correction shows up on the very next page load, no
regenerate needed.

**Everything here is audited.** Each change is written to the access record
against your name, including a correction *back* to "not recorded".

> ### ⚠️ Be aware
>
> - **An internal (APSA/CUPE) uplift cannot rescue an ineligible candidate.** If
>   both apply, the band wins and both facts are shown.
> - **The note field only exists on the candidate page**, not on the card.
> - **Setting it on a card reloads the whole shortlist page.** That is expected,
>   not a fault.

---

## 7. Generating the shortlist

### Preconditions

From the job page, click **View shortlist**. The **Generate shortlist** button
is enabled only when both are true:

1. **At least one résumé shows `parsed`.** Otherwise the button is disabled with
   the tooltip "Upload résumés and wait for them to parse first", and a hint
   below.
2. **The JD carries at least one requirement** (required or nice-to-have).
   Otherwise the button is disabled and an orange banner explains: *"This job
   description yielded no requirements at all… The manager's additional
   requirements alone are not enough to rank against. Re-parse or replace the
   JD."*

### What happens

Press **Generate shortlist** (or **Regenerate**, if a list already exists). The
card area is replaced by a progress message that refreshes every 3 seconds:

> *polling… Generating… ranked candidates will appear here. The local model runs
> on every candidate résumé the run considers, so a full pass realistically takes
> several minutes and scales with the number of résumés involved.*
> `~123s elapsed`

On a **Regenerate**, the previous candidates stay on screen underneath, clearly
labelled *"from the previous run — a new run is in progress and will replace this
list automatically."*

**How long:** minutes, scaling with the pool size. There is no measured figure to
quote here.

### If it does not finish

- **The page gives up after about 20 minutes** of polling (400 checks at 3
  seconds). It then says so honestly — no spinner, no promise — and tells you to
  check back later or press **Regenerate**.
- If the AI call failed, you get a distinct message naming the recorded reason,
  and a warning that this is **not necessarily temporary**: the same input
  produces the same answer, so if the same reason keeps coming back, waiting will
  not clear it.

### Two lines you will see above the cards

- **"N of M candidates have no work-authorization declaration."** A count, not a
  warning — it tells you how much of the pool is undeclared without opening every
  card.
- **"N résumés have a degraded parse and are not ranked — open each résumé to
  see why; a re-upload re-parses it."** These candidates are **not in the list at
  all**. This line is the only thing that tells you they are missing.

> ### ⚠️ Be aware
>
> - **"Generate shortlist" queues behind every résumé parse.** The work queue is
>   first-in-first-out. If you press Generate while a large batch is still
>   parsing, the request succeeds, the page says "Generating…", and then it sits
>   for **hours** — because ~1,400 parse jobs are ahead of it. This exactly
>   reproduces the pilot complaint *"Generate shortlist has not been producing
>   anything"*. **Wait for the résumé table to go quiet before generating.**
> - **The page cannot show you the queue depth.** There is no "you are 900th in
>   line" indicator. "Several minutes" is what it says regardless.
> - **A second Regenerate during a run is silently dropped** with no message.
> - **A run that takes longer than an hour** may be reported as stale by the
>   page's own staleness check even though it is still going.

---

## 8. Reading the shortlist

### The card

Each candidate is a card. Top row, left to right:

- **Rank** (`#1`, `#2`, …) — or **—** for an ineligible candidate.
- **The candidate's name** on a normal job, or **"Candidate A" / "Candidate B"**
  on a blind job. It links to the candidate page.
- **Reveal identity (audited)** — only on a blind job.
- **Withdraw candidate**.
- **The headline score**, 0–100 — or **n/a** for an ineligible candidate.

### What the score actually means

The final score is a weighted blend of four things:

| Component | Weight |
|---|---|
| **Structured** — the deterministic match against the posting | **60%** |
| **Evidence** — verified quotes from the résumé against each requirement | **30%** |
| **Manager's additional requirements** | **10%** |
| **Motivation** (cover letter) | **0%** |

The cover letter is **identified but never ranked** — that 10% was moved onto the
manager's own requirements at the sponsor's instruction. Whether a cover letter
exists appears as a column in the CSV export.

The **Structured** 60% is itself made of five sub-scores, shown as tiles on
each card (0–100 each):

| Tile | Weight within Structured | What it measures |
|---|---|---|
| **skill** | **40%** | Required and nice-to-have skills matched |
| **experience** | **25%** | Years against the job's stated minimum |
| **education** | **10%** | Degree level and field against the posting's bar |
| **seniority** | **15%** | Job-title seniority against the posting's |
| **vector** | **10%** | Overall semantic similarity of résumé to posting |

A missing **must-have** skill applies a penalty. A candidate who is
dramatically over-qualified is gently discounted, with a floor.

### Chips under the tiles

- **Skill chips** — green for matched, red-outlined for `— missing`, with
  `· must-have` marked. These come from the **posting**.
- **"Added by you:"** chips — a separate row, present only when the job carries
  additional requirements. These are the **manager's** requirements, marked
  `· required` or not. The separation is the point: you can see whether what
  *you* asked for was actually considered.
- **SFU internal +5** — a chip reading e.g. `SFU internal (APSA) +5`, showing the
  uplift this candidate actually received.

### Evidence

Each card has a collapsible **Evidence** section. Inside, per requirement:

- A badge: **met**, **partial** or **missing**.
- The requirement text.
- A **direct quote from the résumé** supporting it, where one was found.
- The chunk identifiers it came from, and a further collapsible **source text**
  showing the surrounding résumé text (already redacted on a blind job).
- An overall summary line.

**"Met" means a quote was found and verified.** Any quote that failed
verification is shown as **missing**, never as met — the product will not credit
a citation it could not confirm.

### Revealing an identity (blind jobs)

Press **Reveal identity (audited)** on a card, or on the candidate page. The
page re-renders with the real name, email, phone and location, and a line
reading *"Identity revealed — recorded in the audit log."*

**Every reveal is written to the access record** with who, what, when and which
screen it was triggered from. It is a deliberate act, not something a browser can
do by accident.

### Withdraw and reinstate

- **Withdraw candidate** on a card removes them from every shortlist
  immediately, keeps the record, and returns you to the shortlist.
- On the **candidate page** the same control offers an optional **Reason** (up to
  500 characters). The page warns you that the reason is not shown in the access
  record by default, **but an auditor can ask to see it — and that request is
  logged under their name.**
- A withdrawn candidate's page then shows a **Reinstate** button.

### Exports

Three buttons at the top of the shortlist:

| Button | Contents |
|---|---|
| **Export CSV** | One row per candidate: rank, name, email, phone, résumé file, job title and department, work authorization, whether metrics are invalidated, whether a cover letter exists, final score, missing must-haves, missing/matched skills and counts, the three top-level sub-scores, the five structured sub-scores, requirement counts met/partial/missing, the evidence summary, generated-at, pipeline version, résumé id |
| **Export evidence CSV** | One row per *(candidate, requirement)*: rank, name, résumé file, job title, requirement, status, confidence, the quote, chunk ids, and the surrounding source text |
| **Export JSON** | The same underlying rows, unformatted |

> ### ⚠️ Be aware
>
> - **Every export is anonymised, always — even on a non-blind job.** The
>   `candidate_name` column contains "Candidate A", "Candidate B" and so on;
>   `candidate_email` and `candidate_phone` are **blank**; the résumé filename is
>   masked to its extension. There is no option anywhere to export real
>   identities. Use the `resume_id` column plus the on-screen list to tie a row
>   back to a person. **This surprises people; plan for it.**
> - **The tiles and the CSV show a bare number where the detail page would say
>   "not assessed."** A `0` on the `seniority` or `education` tile may mean
>   "could not be measured", not "measured and poor". The honest version of that
>   distinction lives on the "Why this rank?" page — see the next bullet.
> - **The "Why this rank?" page cannot be reached by clicking.** The product
>   contains a full score-composition and disclosure page per candidate (weights,
>   score, weight × score contribution, and explicit "not assessed" paragraphs
>   for evidence beyond the cut-off, unmeasurable seniority, an unstated
>   experience or education bar, and unreadable education). **Nothing links to
>   it**, and the entry identifier it needs is not in any export. Today it is
>   effectively unavailable to a user.
> - **Evidence is only extracted for the top 15 candidates** by structured score.
>   Everyone below that has evidence and motivation stored as 0, which on a card
>   looks like a measured zero. Their headline scores are therefore **not
>   comparable** with the scores above the cut-off.
> - **On a shortlist with more than about 32 cards, "Reveal identity" starts
>   returning an error** on the earliest cards. The per-card security tokens are
>   capped at 64 per session and the oldest are dropped. Pilot shortlists have
>   been under 10, but the default shortlist size is 100%, so a 50-candidate job
>   will hit it. Reloading the page re-issues them.
> - **A quick withdraw from a card records no reason** — only the candidate page
>   collects one.

---

## 9. The candidate page

Reached by clicking a candidate's name on a shortlist, or a row in the job's
résumé table.

### What you see

- The **original filename** and the parse status, plus a **degraded** badge and
  explanation if the AI extraction fell back to a keyword scan.
- **Identity** — shown directly on a non-blind job; hidden behind the audited
  **Reveal identity** button on a blind one.
- **Withdraw** (with an optional reason) or, if already withdrawn, the reason
  and a **Reinstate** button.
- **Download résumé (audited)** and, when one exists, **Download cover letter
  (audited)**. These are **buttons, not links**, deliberately: fetching the file
  discloses the candidate's identity, so it is treated as a reveal and recorded
  against your name whether or not the review is blind.
- **Eligible to work in Canada** — the three radios plus an optional note (§6).
- The parse summary and total years of experience.
- **Find matching jobs** / **View match results** — the reverse direction: which
  *other* open requisitions this candidate ranks well against. Real job titles
  are shown here (jobs are not personal data). It polls like the shortlist does.
- **Skills** — chips colour-coded by recency: **current** (used within 2 years),
  **aging** (3–5), **stale** (6+), each with the year.
- **Experience** — roles, companies, dates and bullet points.
- **Education** — degree, institution, field, year.
- **Cover letter** — sentiment, themes, key claims and the text, when present.
- **Source chunks** — a collapsible dump of the résumé text the system indexed,
  already redacted on a blind job.

> ### ⚠️ Be aware
>
> - **The résumé and cover-letter download buttons share one security token per
>   page load.** Downloading one, then the other, fails on the second press.
>   Reload the page between downloads.
> - **There is no re-parse for a résumé.** A degraded or failed parse can only be
>   recovered by uploading the file again.
> - **The page does not show the candidate's SFU-internal (APSA/CUPE) flags.**
>   Those appear only as a chip on the shortlist card.

---

## 10. The access record (audit log)

### Who can see it

**Admins and auditors only.** The **Access record** link appears in the header
for those two roles; anyone else gets a 403.

### What you see

A table, newest first, 100 rows per page, with **Newer** / **Older** paging and
an **Action** filter dropdown. Columns: When, Who, Action, Subject, Job, Context,
Details.

"Who" is the person's username. Where an action was performed by the system
rather than a person, it reads `service: <name>` in grey, explicitly — a blank
would read as missing data rather than as a finding.

### What is recorded

Identity reveals; audit-note reveals; candidate withdrawals and reinstatements;
role changes; job assignments and unassignments; work-authorization
declarations; SFU-internal status changes; document downloads; blind-review
toggles; roster reconciliations; Taleo sync runs; and every single-record read
performed by an **auditor** (jobs, résumés, shortlist entries, exports) — the
watchers are watched.

### Withheld notes

Some values show as **withheld**. Those are free-text notes that can name a
candidate or describe their circumstances. Where a **Reveal note** button
appears, you can read one: the note is shown to you, and the request is written
into this same record naming you and the row you opened. Reloading the page hides
it again. Notes with no button are not readable from this page at all.

Today, the only note kind that can be revealed this way is a **withdrawal
reason**.

**Nothing on this page changes anything else in the product.**

> ### ⚠️ Be aware
>
> - **The Action dropdown lists only seven of the recorded action types.**
>   Downloads, work-authorization declarations, blind-review toggles, roster
>   imports and internal-status changes are recorded and *displayed*, but are not
>   offered as filter options. Use "All actions" and page through.
> - **There is no date filter and no search.** Only the action filter and paging.
> - **The record outlives the data.** When pilot data was wiped, the access
>   record was deliberately kept — so it still references candidates whose
>   résumés no longer exist.
> - **Reveals are not rate-limited.** The record is the control: it documents
>   access rather than preventing it, and nothing alerts on an unusual pattern.

---

## 11. Privacy and data handling, in plain words

**Everything stays on SFU infrastructure.** The application, the databases and
the AI model all run on SFU-controlled machines. The model itself runs on a GPU
host reached over the private Tailscale network.

**No candidate data ever leaves that boundary.** No résumé, no cover letter, no
shortlist, no prompt and no candidate detail is sent to any cloud AI service.
This is a structural property of the system, not a setting.

**There is exactly one permitted outbound connection, and it is switched off.**
The Taleo job-import feature may read SFU's own public job postings from
`tre.tbe.taleo.net`, and nothing else. It is disabled by default and remains
disabled on this deployment; enabling it needs a firewall rule, counsel and
privacy-officer sign-off, and a named owner.

**Candidate identifiers are encrypted at rest.** Names, email addresses, phone
numbers and cover-letter text are stored encrypted in the database. Only a
one-way hash of the email is stored unencrypted, for matching.

**Consent is collected at upload.** Every résumé upload requires an explicit
PIPEDA/FIPPA acknowledgement; without the tick, nothing is sent.

**Blind review is available per requisition** and hides identity across the
review screens, with identity reachable only through an audited reveal.

**Screening facts are declared, never guessed.** Work authorization and SFU
internal status come from the candidate's own Taleo answer or a recruiter's
explicit, audited declaration. The AI is never asked to infer either from a
résumé — immigration status is a protected ground, and a guess there is an
adverse decision nobody made.

**The roster CSV is not persisted.** It is parsed in memory, matched, and
discarded.

> ### ⚠️ Be aware — what is *not* in place
>
> - **Retention is stored and never enforced.** Each requisition carries a
>   retention period (default 180 days), and **nothing deletes anything when it
>   expires.** No purge job exists. Candidate documents stay until someone
>   removes them by hand. With real candidate data on a real box this is a
>   commitment the product makes and does not currently keep.
> - **There is no candidate-initiated erasure path.** "Revoke and purge" has been
>   deferred pending an HR decision.
> - **Uploaded files themselves are not encrypted at rest** — they are protected
>   by file-system permissions only. The database columns above are encrypted;
>   the stored PDFs are not.
> - **The access record is immutable by convention, not by the database.**
> - **There are no backups and no restore drill.**
> - **Session records log the proxy's address, not the real client IP.**
> - **A known external exposure is still open:** real candidate résumés committed
>   to a public code repository earlier in the project remain fetchable by
>   identifier, and a support request to purge them has not been completed. This
>   is the oldest unresolved privacy item.

---

## 12. Known limitations — the honest list

Read this before you conclude something is broken.

**Speed**

- **Parsing is hardware-bound at ~29 résumés/hour.** 315 candidates is about 11
  hours. Feature work was paused on this basis until more GPU capacity is
  available. Tuning the queue buys perhaps 30%, not 10×.
- **"Generate shortlist" queues behind every parse.** With a large batch in
  flight, Generate can sit for hours while the page says "several minutes".
  Wait for parsing to finish.
- **No progress indicator shows the queue depth.**

**Missing screens and controls**

- **No notifications of any kind** — no email, no in-app badge, no "your
  shortlist is ready". You must come back and look. (An in-app notification
  centre is designed but not built.)
- **No way to assign a requisition to a hiring manager** from the interface, so
  the hiring-manager role currently shows an empty job list.
- **No way to edit a job's description** after creation, which is also why a JD
  that parsed to zero requirements has no in-product recovery — create the job
  again.
- **No Re-parse for a résumé** — re-upload instead.
- **No way to refresh a job's search index without re-running the whole JD
  parse**, which can change the extracted requirements underneath a shortlist
  someone is reading.
- **The "Why this rank?" explanation page is not linked from anywhere.**
- **Shortlist size (%) can only be set at creation.**

**Data quality**

- **Department is free text and will not group or filter reliably.**
- **Evidence is only extracted for the top 15 candidates**; everyone below shows
  a stored 0 that a card cannot distinguish from a measured zero.
- **The same "measured zero vs. not measured" ambiguity** applies to the
  seniority, education, experience and vector tiles on a card and to the CSV.
- **A scanned or image-only PDF will not extract**; paste the text instead.
- **Windows-1252 text files** produce garbled characters throughout.

**Access and security**

- **Use `https://sfuai.ca:8000`.** There is no HTTP→HTTPS redirect and no
  port-less address; the router does not forward port 80 or 443 to this box.
- **Any SFU CAS user can sign in**, creating a no-role account. They can do
  nothing until an admin grants a role.
- **Reveal buttons fail on shortlists beyond ~32 cards** until the page is
  reloaded.
- **Exports are anonymised with no opt-out.**
- **Retention is not enforced** (see §11).

---

# Appendix — where each statement comes from

*For engineers maintaining this guide. Non-technical readers can stop here.*
All paths are relative to the repository root.

| Guide section | Sources |
|---|---|
| §0 checklist | `HANDOFF.md:117-126` (URL/TLS), `HANDOFF.md:167-197` (ordering, throughput, Windows-1252), `docs/adr/025-user-admin-roles.md` (no role on first login), `core/src/services/bulk_ingest_service.py:506-527` (roster columns) |
| §1 sign-in, CAS gate, pending access | `core/frontend/app.py:196-231`, `core/frontend/templates/pending_access.html`, `core/frontend/templates/base.html:15-37` |
| §1 role capabilities | `core/frontend/app.py:313` (`_WRITER_ROLES`), `:409` (`_ASSIGNABLE_ROLES`), `:412-436` (admin page), `:443,469-489` (audit page), `:495-530` (hiring-manager scoped list); backend: `core/src/api/routes/jobs.py:54`, `resumes.py:77,84`, `shortlist.py:38`, `audit.py:40`, `job_assignees.py:54`; `core/src/api/deps.py:466-496` (auditor reads are logged) |
| §1 granting a role, last-admin guard | `core/frontend/app.py:1933-1945,2064-2092`, `core/frontend/templates/admin_users.html`, `docs/adr/025-user-admin-roles.md` §3–§4 |
| §1 no assignee screen | `core/src/api/routes/job_assignees.py` exists; no reference in `core/frontend/` (grep for "assignee" returns nothing) |
| §2 create form | `core/frontend/templates/index.html:14-109`, `core/frontend/app.py:533-570`, `core/src/schemas/jobs.py:61-102` |
| §2 blind review off by default | `core/src/schemas/jobs.py:83-88` |
| §2 shortlist top percent | `core/src/schemas/jobs.py:79`, `core/frontend/templates/index.html:52-56`, `core/src/pipeline/matching/orchestrator.py:922-935` |
| §2 JD prefill formats/size | `core/frontend/app.py:573-591`, `core/src/services/jd_import_service.py:30,51-94` |
| §2 additional-requirements box | `core/frontend/templates/index.html:71-99`, `core/frontend/templates/job_detail.html:118-194`, `core/frontend/app.py:1115-1150`, weight at `core/src/schemas/matching.py:211-216` |
| §2 bulk upload + CSV manifest | `core/frontend/templates/index.html:111-135`, `core/frontend/app.py:621-670`, `core/src/api/routes/jobs.py:63,66,117-178`, `core/src/services/bulk_ingest_service.py:305-422` |
| §2 Taleo off | `core/frontend/templates/index.html:137-158`, `core/frontend/app.py:1597-1624`, `core/src/settings.py:346-352`, `docs/adr/046-taleo-job-source-egress-carveout.md` §Consequences |
| §2 department/campus edit | `core/frontend/templates/job_detail.html:39-87`, `core/frontend/app.py:1066-1112` |
| §2 department free text | `docs/ROADMAP.md:209` |
| §3 parse states + polling | `core/frontend/templates/parse_status.html`, `core/frontend/templates/job_detail.html:19-31,196-219` |
| §3 zero-requirements warning | `core/frontend/templates/parse_status.html:36-43`, `core/src/services/shortlist_service.py:246-272` |
| §3 re-parse conditions | `core/frontend/templates/job_detail.html:212-219`, `core/frontend/app.py:1153-1174`, `core/src/api/routes/jobs.py:346-382` |
| §3 no description edit | `core/frontend/app.py:1060-1066` (`_EDITABLE_DETAIL_FIELDS`), `:1137` |
| §3 status transitions / open gate | `core/frontend/app.py:712-724`, `core/frontend/templates/job_detail.html:221-254,273` |
| §3 blind toggle | `core/frontend/templates/job_detail.html:256-266`, `core/frontend/app.py:1177-1194` |
| §4 upload form + consent | `core/frontend/templates/job_detail.html:273-316`, `core/frontend/app.py:830-904` |
| §4 limits | `core/src/api/routes/resumes.py:88-127`, `core/src/services/resume_service.py:64-72,345-393`, `core/src/services/zip_upload.py:28-33` |
| §4 pairing conventions | `core/src/services/bulk_ingest_service.py:40-98,125-194,250-290` |
| §4 manifest not inside zip | `core/src/api/routes/resumes.py:137-146` |
| §4 results summary | `core/frontend/app.py:907-931` |
| §4 status table / breakdown / degraded | `core/frontend/templates/resumes_table.html`, `resume_status_breakdown.html`, `core/frontend/app.py:796-802` |
| §4 throughput | `HANDOFF.md:171-183`, `docs/ROADMAP.md:49-70` |
| §5 ordering dependency | `HANDOFF.md:167-170` |
| §5 form + report | `core/frontend/templates/job_detail.html:318-337`, `core/frontend/app.py:934-994` |
| §5 CSV parsing + mapping | `core/src/services/bulk_ingest_service.py:435-588`, esp. `WORK_AUTHORIZATION_MAP:464-469` |
| §5 matching + refusals | `core/src/services/candidate_roster_service.py:1-44,180-320,327-457` |
| §5 overwrite behaviour | `core/src/services/candidate_roster_service.py:353-368`, `core/src/services/resume_service.py:773-776,1037-1098`, `docs/adr/047-screening-facts-are-declared-never-inferred.md` §C |
| §5 CSV not persisted | `docs/adr/047-...md:135-148` |
| §5 full-export vs subset | `HANDOFF.md:192-194` |
| §5 uplift amount | `core/src/settings.py:281-297`, `core/src/pipeline/matching/stages.py:772-800` |
| §6 three states / macro | `core/frontend/templates/_work_auth_fieldset.html:1-27` |
| §6 controls | `core/frontend/templates/resume_detail.html:203-248`, `shortlist_cards.html:308-364`, `core/frontend/app.py:1678-1770` |
| §6 band behaviour | `core/frontend/templates/shortlist_cards.html:196-212,294-306,366-377`, `core/src/schemas/matching.py:724-770` |
| §6 read-time band | `core/src/schemas/matching.py:728-733` |
| §7 preconditions | `core/frontend/templates/shortlist_list.html:21-60`, `core/frontend/app.py:1230-1290` |
| §7 polling / give-up / states | `core/frontend/templates/shortlist_cards.html:18-192`, `core/frontend/app.py:737,1320-1392` |
| §7 undeclared count | `core/frontend/templates/shortlist_cards.html:90-109` |
| §7 degraded excluded | `core/frontend/templates/shortlist_list.html:61-78` |
| §7 queue behind parses | `HANDOFF.md:184-191` |
| §7 dropped second regenerate / staleness | `docs/ROADMAP.md:217-218` |
| §8 weights | `core/src/schemas/matching.py:195-340` (`MatchWeights` defaults, `_sums_close_to_one`) |
| §8 tiles + chips + manager chips | `core/frontend/templates/shortlist_cards.html:366-417`, `core/src/schemas/matching.py:357-445` |
| §8 internal chip | `core/frontend/templates/shortlist_cards.html:266-292` |
| §8 evidence panel + verification | `core/frontend/templates/shortlist_cards.html:419-449`, `core/frontend/templates/shortlist_entry.html:225-233` |
| §8 reveal / withdraw / reinstate | `core/frontend/app.py:1516-1594,1773-1794`, `core/frontend/templates/resume_detail.html:47-157` |
| §8 export buttons + columns | `core/frontend/templates/shortlist_list.html:11-19`, `core/frontend/app.py:1887-1930`, `core/src/services/shortlist_service.py:1348-1392,1457-1470` |
| §8 exports always anonymised | `core/src/services/shortlist_service.py:1170-1194`, `core/src/api/routes/shortlist.py:87-110` |
| §8 "Why this rank?" unreachable | page exists at `core/frontend/app.py:1395-1443` + `core/frontend/templates/shortlist_entry.html`; no template references `shortlist_entry_detail`; entry id absent from `_EXPORT_QUERY` (`core/src/services/shortlist_service.py:1041-1066`) |
| §8 evidence cliff | `core/src/settings.py:240` (`match_evidence_k = 15`), `core/frontend/templates/shortlist_entry.html:80-97`, `docs/adr/040-evidence-cliff-disclosure.md` |
| §8 not-assessed markers | `core/src/schemas/matching.py:399-430`, `core/frontend/templates/shortlist_entry.html:135-223`, `docs/adr/041-sub-score-measurement-markers.md` |
| §8 card/CSV show bare numbers | `docs/ROADMAP.md:204` |
| §8 32-card token cap | `docs/ROADMAP.md:239`, `core/frontend/app.py:1197-1227,1744-1752` |
| §8 card withdraw has no reason | `docs/ROADMAP.md:237` |
| §9 résumé page | `core/frontend/templates/resume_detail.html` (whole file), `core/src/schemas/resumes.py:504-543` |
| §9 downloads audited, shared token | `core/frontend/templates/resume_detail.html:159-201`, `core/frontend/app.py:1627-1675` |
| §9 no résumé re-parse | `docs/ROADMAP.md:220` |
| §10 audit viewer | `core/frontend/templates/audit_log.html`, `core/frontend/app.py:1948-2061` |
| §10 action list shown in filter | `core/frontend/app.py:452-460` vs. actual `record_audit` actions across `core/src/services/*.py` and `core/src/api/routes/*.py` |
| §10 withheld / revealable | `core/src/services/audit_service.py:40-89,115-122,266-276` |
| §10 record outlives data | `HANDOFF.md:95-102` |
| §10 reveals not rate-limited | `docs/ROADMAP.md:238` |
| §11 offline inference | `CLAUDE.md` §Stack, `docs/adr/003-offline-inference-ollama.md` |
| §11 single egress carve-out | `docs/adr/046-taleo-job-source-egress-carveout.md`, `core/src/settings.py:346-352` |
| §11 PII encryption | `core/src/models/ddl.py:26-28,337-349` |
| §11 consent | `core/frontend/app.py:830-842`, `core/frontend/templates/job_detail.html:306-311` |
| §11 declared-never-inferred | `docs/adr/047-screening-facts-are-declared-never-inferred.md` |
| §11 retention not enforced | `core/src/models/ddl.py:137-138` defines it; `docs/ROADMAP.md:111-117` records that no purge path reads it |
| §11 blobs unencrypted, no backups, audit immutability | `docs/ROADMAP.md:119-125` |
| §11 proxy IP in sessions | `docs/ROADMAP.md:233-235`, `docs/deploy/sfuai-ca.md` |
| §11 public-repo PII exposure | `docs/ROADMAP.md:236` |
| §12 no notifications | `docs/SPONSOR_REQUIREMENTS_PLAN.md:296-328`; no `notifications` table in `core/src/models/ddl.py`, no route, no template |
| §12 no re-project control | `docs/ROADMAP.md:213` |
| §12 no HTTP redirect, port 8000 | `docs/deploy/sfuai-ca.md:3-4,159-168`, `HANDOFF.md:117-126` |
| §12 any CAS user can authenticate | `docs/ROADMAP.md:227-228` |
| §12 scanned PDFs | `core/src/services/jd_import_service.py:88-93` |
