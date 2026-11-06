# rassi-extract

This tool reads scanned RASSI answer sheets and turns each student's marks into spreadsheet files (CSVs). It sends each scanned page to OpenAI's API, which reads the marks, and then checks and organizes what comes back.

It only ever works on scans that have already been de-identified with melredact and approved by a person. Student names never enter this tool. You copy approved, redacted PDFs into `data/in` by hand.

## One-time setup

You need Python 3.11 or newer. Open a terminal in the `rassi-extract` folder and run:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Every time you open a new terminal, run `source .venv/bin/activate` again before using the tool.

### Setting your OpenAI API key

The tool needs an OpenAI API key to call the model. Keep the key in a file named `.env` in the `rassi-extract` folder, next to this README. The file holds one line:

```
OPENAI_API_KEY=sk-...your key here...
```

Replace everything after the `=` with your own key, with no quotes and no spaces. `.env` is already listed in `.gitignore`, so it is never committed. Never paste the key into code, a CSV, a chat or an email. The `--dry-run` option works without a key.

## Where files go

```
data/
  in/
    020421/
      020421_PD1_RASSI.pdf
      020421_PD2_RASSI.pdf
    010401/
      010401_PD3_RASSI.pdf
  out/                                  upload this folder to Box
    020421/
      PD1/
        020421_PD1_RASSI.csv
        020421_PD1_RASSI_flags.csv
        020421_PD1_RASSI_review.txt
      PD2/
        ...
    _combined/
      all_students.csv
      all_review.txt
  details/                              keep on this computer, for checking runs
    020421/
      PD1/
        020421_PD1_RASSI_long.csv
        020421_PD1_RASSI_metrics.csv
        020421_PD1_RASSI_manifest.json
        raw/
          020421_PD1_RASSI_page01.json
          020421_PD1_RASSI_page02.json
          ...
      PD2/
        ...
```

Put each PDF in the folder for its teacher code. The file name must be exactly `<teacher_code>_PD<period>_RASSI.pdf`, for example `020421_PD1_RASSI.pdf`. The teacher code in the file name must match the folder it sits in. Files that break these rules are skipped, and the tool tells you why.

Results are split into two folders, both named after the PDF:

- **`data/out/<teacher_code>/PD<period>/`** holds the files people use: the student CSV, the flags CSV and the review log. **Upload `data/out` to Box.** After every run, `data/out/_combined/` is rebuilt from the per file results so it always matches them.
- **`data/details/<teacher_code>/PD<period>/`** holds the files for checking how a run went: the long CSV, the metrics CSV, the manifest and the raw model answers. Keep these on this computer. Do not upload them to Box.

Nothing in `data/in`, `data/out`, `data/details` or `cache/` is committed to git.

### What the tool expects inside each PDF

- Two pages per student, in order: student A page 1, student A page 2, student B page 1, and so on.
- Page 1 has a black box across the top with white text reading `SID: <id>` and `PD: <period>`, or `REDACTED` if no SID was known. Page 2 has no box. The tool links page 2 to a student only by its position right after that student's page 1.
- Items 1 to 6 are on page 1 and items 7 to 12 on page 2.

If the pages come back in an order that breaks this pattern, the tool stops before writing student CSVs. It reports the problem as a structure violation, described below.

## Running it

Every command starts with `python -m rassi extract`.

Process every teacher folder in `data/in`:

```
python -m rassi extract
```

### Options

| Option | What it does | Example |
|---|---|---|
| `--teacher CODE` | Process only one teacher folder. | `python -m rassi extract --teacher 020421` |
| `--file PATH` | Process only one PDF. The path must be inside `data/in`. You cannot combine this with `--teacher`. | `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf` |
| `--pages RANGE` | Process only some pages of a PDF. Use it with `--file`, or with a `--teacher` folder that holds exactly one PDF. Keep whole students together: use ranges that start on an odd page and end on an even page. | `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --pages 1-4` |
| `--dry-run` | Opens the PDF and shows the page count, image sizes and estimated cost, without calling the API or spending money. It writes nothing to `data/out` or `data/details`. Always do this first on a new file. | `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --dry-run` |
| `--max-spend DOLLARS` | Spending limit in US dollars for the whole command, across every file it processes. Default `2.00`. The tool stops starting new pages before it would go over. | `python -m rassi extract --teacher 020421 --max-spend 5.00` |
| `--model NAME` | Which OpenAI model to use. Default `gpt-5.6-terra`. `gpt-5.6-luna` is cheaper but less accurate. | `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --model gpt-5.6-luna` |
| `--workers N` | How many pages to send to the API at the same time. Default `4`. Lower it to `1` if you hit rate limits. | `python -m rassi extract --teacher 020421 --workers 2` |
| `--overwrite` | Replace results that already exist in `data/out` or `data/details`, including files in the older layout such as `<file name>_wide.csv` or a `raw/` folder in `data/out`. Without it, a file that already has results is skipped, so you never lose results by accident. | `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --overwrite` |

`--pages` accepts a single page (`5`), a range (`1-4`) or several ranges separated by commas (`1-2,5-6`).

### A safe way to process a new PDF

1. Dry run the whole file and check the page count and estimated cost:
   `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --dry-run`
2. Run the first two students only, with a small spending limit, and check the CSVs against the paper sheets:
   `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --pages 1-4 --max-spend 0.50`
3. If they look right, run the whole file:
   `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --overwrite`

In step 3, the four pages from step 2 come from the cache (see below) and cost nothing.

### Reruns and the cache

Every page the model reads successfully is saved in `cache/`. If you run the same page again with the same model and the same instructions, the saved answer is reused, at no cost. Pages that failed are never cached, so a rerun tries them again. A change to the model instructions creates a new cache entry, so pages are read again under the new instructions.

## Finding the values to check

This section is for anyone working with the results in a spreadsheet.

**Two kinds of value to check.**
- A **double entry** is a row where the student marked more than one option. The cell holds every marked option joined by commas, such as `c,d` or `1,4`. The student may have meant both, or changed their mind without erasing, so someone should decide which answer to keep.
- An **uncertain value** is one the reader could not read with certainty, such as a faint mark or one circle touching two options. It ends in `?`, such as `4?`, `c,d?` or `ambiguous?`. Use your spreadsheet's Find (Ctrl+F or Cmd+F) and search for `?` to find them.

**`c,d` and `c,d?` are different.**
- `c,d` is a double entry the reader was confident about: the student drew two separate circles, one on `c` and one on `d`, and each circle clearly sits on one option. The marks are certain. What the student meant is not, so it still needs a look.
- `c,d?` is a double entry the reader was unsure of: for example one circle drawn around `c` and `d` together. Both the marks and the meaning need checking. It counts as a double entry and as an uncertain value.
- `ambiguous` always shows as `ambiguous?`.
- A single answer can also carry `?`, for example `4?`, when the one mark was faint, broken or partly hidden by a smudge or shadow.

A value without `?` was read cleanly. Only uncertain values get `?`. A double entry the reader was confident about appears without it, as `c,d`.

**Four columns at the end of each student's row**, after `Q12_C`:

| Column | Meaning |
|---|---|
| `n_multiple` | How many of this student's 36 values are double entries, with or without `?`. |
| `n_uncertain` | How many of this student's 36 values end in `?`. |
| `n_blank` | How many of this student's 36 values are `blank`. |
| `check` | Says `check` when `n_multiple` or `n_uncertain` is 1 or more. Empty otherwise. Sort or filter on this column to see every student who needs a look. |

A value like `c,d?` counts in both `n_multiple` and `n_uncertain`. The counts are kept apart on purpose. A student with double entries marked more than one option. A student with uncertain values made marks that were hard to read. A student with many blanks skipped questions. These are handled differently.

**Blanks are usually not errors.** `blank` means the row had no mark, which nearly always means the student skipped it. `blank` never gets a `?` and never makes `check` appear. Look at blanks only if a skipped row seems unlikely for that student.

**The review log.** Each PDF gets a plain text file, `data/out/<teacher_code>/PD<period>/<file name>_review.txt`, for example `data/out/020421/PD4/020421_PD4_RASSI_review.txt`. It opens with a summary line counting the double entries, uncertain values and blanks, and how many students need checking. Then come three sections, each with a heading and a one line explanation:

1. **Double entries**: every row where the student marked more than one option.
2. **Uncertain values**: every value that ends in `?`. A double entry the reader was unsure of appears here and in the first section.
3. **Blanks**: skipped rows, usually no action needed.

Double entries and uncertain values are grouped by student and in item order. Each line gives the item, the row (Agreement, Reason or Confidence), the value, the page number in the PDF to open, and a short description of what the model saw, such as "one circle around c and d". A section with nothing in it says `None`.

**Everything in one place.** `data/out/_combined/all_students.csv` holds every student from every processed PDF, with the same columns as the per file student CSV. Use the `teacher_code`, `period`, `pre_post` and `source_pdf` columns to tell the files apart. `data/out/_combined/all_review.txt` is every review log in one file, with the PDF's name as a heading above each. Both files are rebuilt after every run, so never edit them by hand. Copy them first if you want to make notes.

## What each output file contains

All files for one PDF start with that PDF's name, for example `020421_PD1_RASSI`.

| File | Folder | Upload to Box |
|---|---|---|
| `<file name>.csv` | `data/out/<teacher_code>/PD<period>/` | Yes |
| `<file name>_flags.csv` | `data/out/<teacher_code>/PD<period>/` | Yes |
| `<file name>_review.txt` | `data/out/<teacher_code>/PD<period>/` | Yes |
| `<file name>_long.csv` | `data/details/<teacher_code>/PD<period>/` | No |
| `<file name>_metrics.csv` | `data/details/<teacher_code>/PD<period>/` | No |
| `<file name>_manifest.json` | `data/details/<teacher_code>/PD<period>/` | No |
| `raw/<file name>_pageNN.json` | `data/details/<teacher_code>/PD<period>/` | No |

**`<file name>.csv`**, for example `020421_PD1_RASSI.csv`: the student CSV, one row per student, in wide format. Earlier versions called it `<file name>_wide.csv`. This is usually the file to analyze.
- `sid`, `teacher_code`, `period`, `form`, `pre_post`, `source_pdf`, `page_start` (the PDF page where this student starts).
- For each item 1 to 12:
  - `Q<n>_A`: Agreement, 1 to 6.
  - `Q<n>_B`: Reason, the letter a to d.
  - `Q<n>_C`: Confidence, 1 to 6.
- When two or more answers are marked in a row, a cell holds all of them joined by commas in left to right order, such as `1,4` or `a,c,d`.
- A cell can also hold `blank` or `ambiguous` instead of an answer. See [Flag values](#flag-values).
- A value ends in `?` when the model was unsure how it was marked, such as `c,d?` or `ambiguous?`. `blank` never has one. See [Finding the values to check](#finding-the-values-to-check).
- `n_multiple`, `n_uncertain`, `n_blank` and `check` at the end of the row, after `Q12_C`. See [Finding the values to check](#finding-the-values-to-check).

**`_long.csv`**: one row per student, per item, per row of the item table (A, B or C). Columns: `sid`, `item`, `dimension` (A, B or C), `value` (the answer as read, never with `?`), `value_marked` (the same answer with `?` added when unsure, as in the student CSV), `mark_confidence` (`clear` or `unsure`), `value_num` (the number for A and C, or empty for `blank`, `ambiguous` or a comma list; always empty for B), `mark_notes` (the model's description of what it saw in that one row of the item table: the A row's note on A, the B row's note on B, the C row's note on C), `page_number`, `model`. Use it to check an individual answer against the scan.

**`_flags.csv`**: everything a person should look at. One row per issue. Columns: `sid`, `page_number`, `item`, `dimension`, `flag_type`, `value`, `reason`. The flag types are:

| `flag_type` | Meaning |
|---|---|
| `value_blank` | No mark in that row. |
| `value_ambiguous` | A mark is there, but which answer it means is unclear. |
| `value_multiple` | More than one answer marked in that row. `value` holds the marked answers joined by commas, such as `1,4`. |
| `sid_missing` | Page 1 has no readable SID. |
| `sid_redacted` | The SID stamp reads REDACTED. |
| `sid_duplicate` | The same SID appears on more than one student in this PDF. |
| `period_missing` | Page 1 has no readable PD stamp. |
| `period_mismatch` | The PD stamp does not match the period in the file name. |
| `pre_post_unreadable` | The PRE or POST label on a page could not be read. |
| `pre_post_student_mismatch` | A student's two pages disagree on PRE or POST. |
| `pre_post_file_mismatch` | A page says PRE or POST differently from the rest of the PDF. |
| `pre_post_file_undetermined` | The tool could not decide whether the PDF is PRE or POST. |
| `page_failed` | A page could not be read at all. Only appears when the run is incomplete. |

**`_metrics.csv`**: one row per page, showing how the run went. Columns:
- `page_number`, `status`, `error`. `status` is `ok`, `failed`, or `skipped_for_spend` (not started because `--max-spend` was reached).
- `sid`: the student's SID. Page 2 rows carry the SID from their page 1.
- `page_type`: `header` for page 1, `continuation` for page 2.
- `latency_seconds`: how long the API call took.
- `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`: how much text the model read and wrote. `cached_input_tokens` is the part of the input that OpenAI had already seen recently, which it bills at a much lower rate.
- `cost_usd`: what the page cost.
- `cache_hit`: `True` if the answer came from this tool's own `cache/` folder at no cost.
- `retry_count`: how many times the API call was retried after a temporary error.
- `model`: the model used.

**`_review.txt`**: the review log described in [Finding the values to check](#finding-the-values-to-check). Written only when the status is `complete`.

**`_manifest.json`**: a record of the run: status, date and time, model, instruction and schema versions, which pages were processed, failed or skipped, token counts, total cost, the prices used, timing, flag counts, any structure violations and, for a complete file, `review_counts` (double entries, uncertain values to check, students to check, blank values). The `status` field tells you whether the other files can be trusted:

| `status` | Meaning |
|---|---|
| `complete` | Every selected page was read and the page order checked out. All CSVs were written. |
| `incomplete` | Some pages failed or were not started because of `--max-spend`. The student and long CSVs were **not** written. Fix the cause and rerun with `--overwrite`. Pages that already succeeded come from the cache for free. |
| `structure_violation` | The pages did not follow the page 1, page 2 pattern, for example a missing page or two page 1s in a row. The student and long CSVs were **not** written. The manifest lists what was wrong. |
| `failed_file` | The PDF could not be opened. The manifest holds the path, the error and the time. Replace the PDF and run again. You do not need `--overwrite` for this. |

**`raw/`**: exactly what the model returned for each page, one JSON file per page. Useful for checking a surprising answer.

## Flag values

Instead of a single answer, a row of an item table can hold one of these. They appear in the student and long CSVs, and each one also gets a row in the flags CSV.

- **`blank`**: the student left the row unmarked, or crossed out their only mark.
- **`ambiguous`**: something is there, but the model could not be sure which answer it means. Examples: a circle with no clear center that sits equally across two numbers, a mark hidden under a scan shadow or smudge, a word written instead of a mark (such as "not sure"), or a mark only in one of the two greyed-out cells of the Reason row. A circle clearly centered on one number is read as that number even when its loop runs into the next cell; the note for that row says so. The greyed-out 5th and 6th cells of the Reason row are never valid answers. They can scan as light grey, dark grey, solid black or striped, and none of that counts as a mark. If a valid letter a to d is clearly marked and there is also a stray mark in a greyed-out cell, the answer is the valid letter and the stray mark is only described in the note.
- **A comma list**, such as `1,4`, `2,3,5` or `a,c,d`: two or more answers are marked in the same row and none is crossed out. Every marked answer is listed, in left to right order. Its flag type is `value_multiple`. A comma list comes either from separate marks that can each be read as sitting in one specific cell, shown without `?`, or from one mark drawn around two or more answers together, shown with `?`, such as `c,d?`. A mark that only touches or sits between cells, without going around the answers, is `ambiguous`, never a comma list.

Every row also gets a mark confidence, `clear` or `unsure`, which decides the `?`:
- `clear`: every marked answer has its own separate mark and each can be read with certainty. A single clean mark is always `clear`, and so is `blank`.
- `unsure`: one mark covers more than one cell, or a mark is faint, broken or partly hidden, or a student's mark sits in a greyed-out Reason cell next to a valid letter. `ambiguous` is always `unsure`.

The model is told never to guess. When in doubt it answers `ambiguous`, and a person should check the scan.

## Cost

The tool shows the cost of every page as it runs, and a summary at the end. Prices per million tokens:

| Model | Input | Cached input | Output |
|---|---|---|---|
| `gpt-5.6-terra` | $2.00 | $0.20 | $12.00 |
| `gpt-5.6-luna` | $0.20 | $0.02 | $1.20 |

OpenAI adds a surcharge to very long requests. This tool sends one page per request, which is far below that limit, so the surcharge never applies. Always dry run first to see the estimate.

## When something goes wrong

The tool prints a message and moves on to the next file when it can. At the end it exits with one of these codes:
- `0`: everything finished.
- `2`: a problem with the command or the input files, such as a wrong file name, a missing folder or a missing API key.
- `3`: at least one file is `incomplete` or `failed_file`.
- `4`: at least one file had a structure violation.

## Measuring prompt accuracy

The `eval` commands check the model instructions against hand labeled examples. They never touch `data/out` or `data/details` and do not change how extraction works.

There are two kinds of example, called cases, and both live side by side in `eval/cases`:

- A **crop** case is one item table cut from a page. It is quick to label, but the model sees much less than it does in a real run, so a crop can pass when the same table fails on the full page.
- A **page** case is one whole page, sent exactly as extraction sends it, with the same instructions. Use page cases for failures seen in real runs.

```
eval/
  cases/
    span-into-neighbour/
      case.json     {"case_type": "crop"}
      crop.png      the cropped item table
      label.json    the correct answer, filled in by a person
      notes.md      one or two lines on why the case is hard
    pd4-page6-vertical-bleed/
      case.json     {"case_type": "page"}
      page.png      the whole page
      label.json    the correct answers for all six items, plus page_type and sid
      notes.md
  results/
    20260930T174721Z_2026-09-30.3.json
```

### Adding a crop case

```
python -m rassi eval add --file data/in/020421/020421_PD1_RASSI.pdf --page 1 --item 6 --case-id span-into-neighbour
```

This finds the item's table by tracing its ruled borders, straightening a skewed scan first, and writes `crop.png`, `case.json` and a `label.json` with only the item number filled in. The PDF must be inside `data/in`. If the tool cannot find exactly six tables on the page, or the item does not belong on that page (items 1 to 6 are on page 1 of a student, 7 to 12 on page 2), it says so and writes nothing. Open the crop, fill in `agreement`, `reason` and `confidence` in `label.json` using the same values as the CSVs (`"1"` to `"6"`, `"a"` to `"d"`, a comma list such as `"1,4"` or `"a,c"`, `"blank"` or `"ambiguous"`, without `?`), and write `notes.md`. The label also has `agreement_mark_confidence`, `reason_mark_confidence` and `confidence_mark_confidence`. Set each to `"clear"` or `"unsure"` to score the model's mark confidence for that row, or leave it `null` to skip it. Case ids use lowercase letters, digits, `-` and `_`. Leave student IDs out of the notes.

### Adding a page case

```
python -m rassi eval add-page --file data/in/020421/020421_PD4_RASSI.pdf --page 6 --case-id pd4-page6-vertical-bleed
```

This renders the whole page at the same resolution extraction uses and writes `page.png`, `case.json` and a `label.json` skeleton with the six item numbers for that page already filled in (items 1 to 6 on odd pages, 7 to 12 on even pages). Open the page and fill in:

- `page_type`: `"header"` for a page with the black SID box, `"continuation"` otherwise.
- `sid`: the SID exactly as stamped, `"REDACTED"` if the stamp says so, or `null` on a continuation page.
- `agreement`, `reason` and `confidence` for every item, with the same values as for a crop case. The three `_mark_confidence` fields are optional, as for a crop case. Add them by hand to an item when you want them scored.

A page case scores 18 values, three for each of its six items, plus any mark confidence fields you filled in. `page_type` and `sid` are checked too and reported on their own line, but they do not count towards the A, B and C accuracy.

For a page with only some answers adjudicated, add `"partial": true` at the top level of `label.json`. Keep `page_type`, `sid`, and all six item numbers. Within each item, supply only the adjudicated answer fields; omitted or null answer fields are unscored, not blank. At least one answer must be labeled. The response must still contain all six correctly numbered items. Without this explicit opt-in, page labels still require all eighteen answers. Mark confidence remains optional. The `pd1-reliability-page*` cases contain the twenty paper-adjudicated cells from the reliability study.

The [2026-10-06 investigation](eval/investigations/2026-10-06-reliability.md) records the visual findings, ten-repeat baseline, four isolated candidate experiments, and per-cell results. None met the reliability criterion; production extraction was not changed. `python -m eval.reliability --help` lists the reproducible experimental variants. These use separate eval cache namespaces.

### Running the evaluation

```
python -m rassi eval run
python -m rassi eval run --repeat 5
python -m rassi eval run --model gpt-5.6-luna --max-spend 0.50
```

Every labeled case is sent to the model with the current instructions. Cases whose `label.json` is still empty are skipped. For each case the tool prints the expected value of every row and how many runs got it right, for example `B=c 4/5`, with a `*` when any run got it wrong and the wrong answers it gave. Mark confidence is scored as its own value, shown as for example `B_mark=unsure 3/5`. When a case has mark confidence labels, its line also says `values PASS, mark confidence FAIL` or similar, so a case can pass on its answers and still fail on how they were marked. A case is only `PASS` overall when both pass. Below that it prints how many cases passed, how many runs passed, accuracy overall and for each of A, B and C, mark confidence accuracy on its own line, the number of unstable values, total cost, mean latency, and which cases changed status since the previous run. The full result, including the model's note on every row of every run, is saved in `eval/results/<time>_<instruction version>.json`.

The model does not always give the same answer for the same image, especially for hard marks. `--repeat N` sends each case N times. A value is **unstable** when the runs did not all give the same answer. An unstable value is a weaker result than a value that was right in every run, even when most runs were right. A case is `PASS` when every run got every value right, `FAIL` when no run did, and `UNSTABLE` when some runs passed and some did not.

Answers are cached in `cache/eval/` by image, model, instruction version and run number, so each of the `--repeat` runs is a separate API call. Rerunning with the same instructions and the same or a smaller `--repeat` costs nothing, and raising `--repeat` sends only the new runs. `--max-spend` defaults to `0.50`. The command exits with `0` when every case passed in every run and `1` otherwise.

## Running the tests

```
python -m pytest
```

The tests never call the real API and cost nothing.
