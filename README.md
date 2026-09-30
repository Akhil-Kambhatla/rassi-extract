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
  out/
    020421/
      020421_PD1_RASSI_wide.csv
      020421_PD1_RASSI_long.csv
      020421_PD1_RASSI_flags.csv
      020421_PD1_RASSI_metrics.csv
      020421_PD1_RASSI_manifest.json
      raw/
        020421_PD1_RASSI_page01.json
        020421_PD1_RASSI_page02.json
        ...
```

Put each PDF in the folder for its teacher code. The file name must be exactly `<teacher_code>_PD<period>_RASSI.pdf`, for example `020421_PD1_RASSI.pdf`. The teacher code in the file name must match the folder it sits in. Files that break these rules are skipped, and the tool tells you why.

Results go to `data/out/<teacher_code>/`, named after the PDF. Nothing in `data/in`, `data/out` or `cache/` is committed to git.

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
| `--dry-run` | Opens the PDF and shows the page count, image sizes and estimated cost, without calling the API or spending money. It writes nothing to `data/out`. Always do this first on a new file. | `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --dry-run` |
| `--max-spend DOLLARS` | Spending limit in US dollars for the whole command, across every file it processes. Default `2.00`. The tool stops starting new pages before it would go over. | `python -m rassi extract --teacher 020421 --max-spend 5.00` |
| `--model NAME` | Which OpenAI model to use. Default `gpt-5.6-terra`. `gpt-5.6-luna` is cheaper but less accurate. | `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --model gpt-5.6-luna` |
| `--workers N` | How many pages to send to the API at the same time. Default `4`. Lower it to `1` if you hit rate limits. | `python -m rassi extract --teacher 020421 --workers 2` |
| `--overwrite` | Replace results that already exist in `data/out`. Without it, a file that already has results is skipped, so you never lose results by accident. | `python -m rassi extract --file data/in/020421/020421_PD1_RASSI.pdf --overwrite` |

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

## What each output file contains

All files for one PDF start with that PDF's name, for example `020421_PD1_RASSI_`.

**`_wide.csv`**: one row per student. This is usually the file to analyze.
- `sid`, `teacher_code`, `period`, `form`, `pre_post`, `source_pdf`, `page_start` (the PDF page where this student starts).
- For each item 1 to 12:
  - `Q<n>_A`: Agreement, 1 to 6.
  - `Q<n>_B`: Reason, the letter a to d.
  - `Q<n>_B_num`: Reason as a number: a=1, b=2, c=3, d=4.
  - `Q<n>_C`: Confidence, 1 to 6.
- A cell can also hold `blank`, `ambiguous` or `multiple` instead of an answer. See [Flag values](#flag-values). `Q<n>_B_num` is left empty in those cases.

**`_long.csv`**: one row per student, per item, per row of the item table (A, B or C). Columns: `sid`, `item`, `dimension` (A, B or C), `value`, `value_num` (the number, or empty for `blank`, `ambiguous` or `multiple`), `mark_notes` (the model's description of what it saw in that one row of the item table: the A row's note on A, the B row's note on B, the C row's note on C), `page_number`, `model`. Use it to check an individual answer against the scan.

**`_flags.csv`**: everything a person should look at. One row per issue. Columns: `sid`, `page_number`, `item`, `dimension`, `flag_type`, `value`, `reason`. The flag types are:

| `flag_type` | Meaning |
|---|---|
| `value_blank` | No mark in that row. |
| `value_ambiguous` | A mark is there, but which answer it means is unclear. |
| `value_multiple` | More than one answer marked in that row. |
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

**`_manifest.json`**: a record of the run: status, date and time, model, instruction and schema versions, which pages were processed, failed or skipped, token counts, total cost, the prices used, timing, flag counts, and any structure violations. The `status` field tells you whether the other files can be trusted:

| `status` | Meaning |
|---|---|
| `complete` | Every selected page was read and the page order checked out. All CSVs were written. |
| `incomplete` | Some pages failed or were not started because of `--max-spend`. The wide and long CSVs were **not** written. Fix the cause and rerun with `--overwrite`. Pages that already succeeded come from the cache for free. |
| `structure_violation` | The pages did not follow the page 1, page 2 pattern, for example a missing page or two page 1s in a row. The wide and long CSVs were **not** written. The manifest lists what was wrong. |
| `failed_file` | The PDF could not be opened. The manifest holds the path, the error and the time. Replace the PDF and run again. You do not need `--overwrite` for this. |

**`raw/`**: exactly what the model returned for each page, one JSON file per page. Useful for checking a surprising answer.

## Flag values

Instead of an answer, a row of an item table can hold one of these three words. They appear in the wide and long CSVs, and each one also gets a row in the flags CSV.

- **`blank`**: the student left the row unmarked, or crossed out their only mark.
- **`ambiguous`**: something is there, but the model could not be sure which answer it means. Examples: a circle with no clear center that sits equally across two numbers, a mark hidden under a scan shadow or smudge, a word written instead of a mark (such as "not sure"), or a mark only in one of the two greyed-out cells of the Reason row. A circle clearly centered on one number is read as that number even when its loop runs into the next cell; the note for that row says so. The greyed-out 5th and 6th cells of the Reason row are never valid answers. They can scan as light grey, dark grey, solid black or striped, and none of that counts as a mark. If a valid letter a to d is clearly marked and there is also a stray mark in a greyed-out cell, the answer is the valid letter and the stray mark is only described in the note.
- **`multiple`**: two or more answers are marked in the same row and none is crossed out.

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

The `eval` commands check the model instructions against hand labeled examples. They never touch `data/out` and do not change how extraction works.

Each example is one item table cut from a page, kept in its own folder:

```
eval/
  cases/
    span-into-neighbour/
      crop.png      the cropped item table
      label.json    the correct answer, filled in by a person
      notes.md      one or two lines on why the case is hard
  results/
    20260930T174721Z_2026-09-30.3.json
```

### Adding a case

```
python -m rassi eval add --file data/in/020421/020421_PD1_RASSI.pdf --page 1 --item 6 --case-id span-into-neighbour
```

This finds the item's table by tracing its ruled borders, straightening a skewed scan first, and writes `crop.png` and a `label.json` with only the item number filled in. The PDF must be inside `data/in`. If the tool cannot find exactly six tables on the page, or the item does not belong on that page (items 1 to 6 are on page 1 of a student, 7 to 12 on page 2), it says so and writes nothing. Open the crop, fill in `agreement`, `reason` and `confidence` in `label.json` using the same values as the CSVs (`"1"` to `"6"`, `"a"` to `"d"`, `"blank"`, `"ambiguous"` or `"multiple"`), and write `notes.md`. Case ids use lowercase letters, digits, `-` and `_`. Leave student IDs out of the notes.

### Running the evaluation

```
python -m rassi eval run
python -m rassi eval run --model gpt-5.6-luna --max-spend 0.50
```

Every labeled case is sent to the model with the current instructions. Cases whose `label.json` is still empty are skipped. The tool prints a table of expected against actual values, with a `*` on each wrong value, then the share of cases that passed, accuracy overall and for each of A, B and C, total cost, mean latency, and which cases changed between pass and fail since the previous run. A case passes only when the item number and all three values match. The full result is saved in `eval/results/<time>_<instruction version>.json`.

Answers are cached in `cache/eval/` by crop, model and instruction version, so rerunning without changing the instructions costs nothing. `--max-spend` defaults to `0.50`. The command exits with `0` when every case passed and `1` otherwise.

## Running the tests

```
python -m pytest
```

The tests never call the real API and cost nothing.
