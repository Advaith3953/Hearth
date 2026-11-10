# Hearth

A private assistant for the files on your own computer, of every type, wherever they are. Ask in plain language:
*"What files are in this folder? Show them by type and the total."*, *"List my PDFs in Downloads."*,
*"What does my lease say about the deposit?"*, *"Get me the Excel files that have a Country column"*, then
*"get the row with Jack in Name"* and *"in that row, change the age to 98."*

How it treats your computer (the short version):

| | Needs your permission? |
|---|---|
| **Listing** files and folders anywhere (names, types, sizes, dates, counts by type) | No. It never opens a file to do this. |
| **Reading** what is inside a file | **Yes**, per file or per folder, each time you start Hearth. Folders you chose with `hearth grant` ("trusted folders") are read without asking. |
| **Changing** anything (create, edit, move, copy, delete, open) | **Yes, always**, one change at a time. And only when your message asks for one: a plain question cannot trigger a change at all (checked in code). |
| Password stores, browser profiles, SSH/cloud keys, Hearth's own data | Never opened, even if you say yes. |
| Windows/system folders | Read with permission; never changed. |

- **Offline.** It talks only to a language model running on your machine (through [Ollama](https://ollama.com)).
  It refuses to contact anything else, and none of its tools can reach the internet.
- **Undo.** Deleted things go to a trash, overwritten files are backed up, and `undo` reverses the last change.
- **Built for a small model.** Works with an ~8B model (for example `llama3.1:8b` or `qwen2.5:7b`). Each step is one small
  JSON action; replies are validated; loops and runaway runs are stopped.
- **No dependencies.** Python 3.10+ and Ollama. The optional extras are `pip install pypdf` (read PDFs) and `pip install pdfminer.six` (copy a PDF into Word *with* its formatting and links). For the extra abilities in 0.4: `pip install pypdf pillow pdfplumber reportlab` (merge/split PDFs, pictures into a PDF, tables out of PDFs, a built-in PDF writer).

> Status: a working prototype. The 0.3 permission model and the new tools have been tested with a scripted stand-in model
> and in a headless browser, **not yet on a real 8B model** (see *How good is it?*). Not yet tried on macOS or Linux desktops.

## Set up (about five minutes)

1. Install [Ollama](https://ollama.com) and a model:  `ollama pull llama3.1:8b`
2. Install Python 3.10 or newer.
3. In this folder:
   ```
   python -m hearth doctor                                    # checks that everything is ready
   python -m hearth ui                                        # opens the web page
   ```
   Optional: choose a **trusted folder**, so that "this folder" means something and Hearth can read it without asking:
   ```
   python -m hearth grant "C:\Users\you\Documents\Notes"    # Windows
   python -m hearth grant ~/Documents/Notes                   # macOS/Linux
   ```
   On Windows you can also double-click `start_hearth.bat` (`start_hearth.sh` elsewhere).
   `pip install .` additionally gives you a plain `hearth` command.

When Hearth wants to read something outside your trusted folders, the page shows a card with three buttons: **Allow this
file**, **Allow this folder until Hearth closes**, **Don't allow**. Pressing *Select* on a file card also counts as allowing
that file. Nothing you allow is remembered after Hearth closes.

## Finding and changing spreadsheets, Word files and PDFs

In the web page (`hearth ui`):

1. **Find.** Type a word in the *Find* bar (for example `Country`) and pick a kind (Excel, Word, PDF, Text), or just ask in the
   chat: *"Get me the excel files which have 'Country' in any of the columns."* For Excel files the word is matched against
   the **column headers** first, then against the cell text. Results appear as file cards that say why each one matched. The Find bar only looks inside folders Hearth may already read (trusted folders, or ones you allowed in the chat).
2. **Select.** Press *Select* on a card. The file opens in a preview on the right, and the chat now says "Working on: people.xlsx".
3. **Ask.** *"Get me the row which has 'Jack' in the 'Name' column."* The matching rows are highlighted in the preview.
   Then *"now in that line replace age 15 with 98."* Hearth remembers which row you mean, shows an Allow card such as
   `Age: 15 -> 98`, and changes only that one cell. If several rows match, it asks you which one.

What works on which file type:

| Type | Find | Read | Change |
|---|---|---|---|
| Excel `.xlsx` | by column header and by cell text | yes, every sheet | **one cell at a time**; everything else in the file is left as it was |
| `.csv` / `.tsv` | same | yes | one cell at a time (delimiter, line endings and BOM are kept) |
| Word `.docx` | by text | yes | find-and-replace and "add at the end"; formatting of everything else is untouched, even when Word split the sentence into several runs |
| PDF | by text (needs `pip install pypdf`) | yes | **no** (read-only); copying it into a new Word file keeps its look (needs `pdfminer.six`) |
| `.xlsm`, old `.xls` / `.doc`, `.pptx` | no | no | no |

Cells that contain a formula are never overwritten. A cell that holds text (an ID such as `00123`) stays text.
Every change is backed up first, so `undo` (or the *Undo last change* button) restores the previous file.

## Everyday use

| You type | What happens |
|---|---|
| `hearth ui` | Opens the chat page at http://127.0.0.1:7083 (this computer only). Changes appear as Allow / Don't allow cards. |
| `hearth chat` (or just `hearth`) | The same in the terminal. |
| `hearth ask "how many files are in Receipts?"` | One question, then exit. |
| `hearth undo` / `hearth history` | Reverse the last change / see recent changes. |
| `hearth grant FOLDER` / `revoke FOLDER` / `folders` | Choose which folders Hearth may read without asking (and what "this folder" means). |
| `hearth model qwen2.5:7b` | Switch model. |
| `hearth cleanup` | Empty trash and backups older than 30 days. |

Questions about new files, copies and moves also offer **Allow all new files in this request**, so a batch ("make 8 copies") needs one click, not eight. It lasts for that one request; deleting, replacing and editing always ask.

`--auto-safe` (on `chat`, `ask`, `ui`) skips the question for harmless, undoable additions: new files, new folders,
copies and moves. Deleting, replacing text and opening files still ask every time.

## What it can do

- **List, anywhere on the computer, with no permission needed:** a folder's contents, a **count of files by type** with the
  total (documents, spreadsheets, PDFs, images, music, video, archives, code, ...), and files found by name or type
  (*"all my photos"*), newest first. Big disks are scanned within a time limit and Hearth says when a result is partial.
  Hidden folders, `AppData`, `node_modules`, links/junctions and system folders are skipped by default.
- **Read, with permission:** search inside files, read text, `.docx`, `.xlsx`/`.csv` and (with `pypdf`) PDF files.
- **Change, always with approval:** create files, make a new Word document (or add to one) from a spreadsheet column, PDF pages or text with `copy_to_word`, turn a Word or text file into a PDF with `save_as_pdf`, edit text (exact find-and-replace, or append), edit single spreadsheet
  cells and `.docx` text, rename/move/copy, delete (to trash), and open a document in your normal app.
- **More jobs (0.4), all asking first when they change anything, all undoable, none deleting or overwriting:**
  `compare_files` (what differs between two files), `find_duplicates` / `move_duplicates` (same content; the oldest stays, the rest go to a
  `_duplicates` folder), `tidy_folder` (sort loose files into folders by type, date or extension, one approval showing the plan),
  `bulk_rename` (find/replace, numbering, prefix/suffix, case; one approval), `pdf_merge`, `pdf_extract` (pages), `images_to_pdf`,
  `fill_template` (fill `{{name}}`-style blanks in a Word file into a new copy), `create_spreadsheet` (new `.xlsx` from CSV, Word tables or PDF
  tables), `zip_contents` and `extract_zip` (look inside or unpack a `.zip` into a new folder), `show_history` and `undo_change`, and
  `find_deadlines` (dates and due-words found in your documents).
- It will not edit PDFs or images, and it can create a new `.docx` but not an `.xlsx`. Copying a **PDF** into a new Word file keeps fonts, sizes, bold/italic, colours, alignment, bullets, dates on the right, the space between lines, rules under headings, underlines, page breaks and hyperlinks (best effort, rebuilt from where the letters sit; with `pdfminer.six` installed). Web and e-mail addresses that are only underlined text in the PDF become real links. Fonts Word does not have are swapped for look-alikes with the same letter widths (Carlito becomes Calibri), so the page count holds. Not copied: images, boxes and backgrounds, real tables; other fonts that are not on the computer are replaced by Word. Copying a **Word** file to a new name is an exact copy. Text added to an existing document, and spreadsheet columns, are plain text; Word files are always saved as `.docx`, so `name.word` becomes `name.docx`. Audio, video, images and programs can be listed and counted, not read.

## How it stays safe

See [SECURITY.md](SECURITY.md) for the full list, including what it does **not** protect against. In short:

- every path is resolved first (`..` and links are not followed) and checked against the rules in the table above;
- listing is metadata only, but it does reveal names and sizes of files everywhere you point it; secret stores
  (`.ssh`, browser profiles, `.kdbx`, ...) are hidden from listings too;
- Hearth's own data folder is unreachable;
- text inside your files is treated as data: a file that says "delete everything" cannot make Hearth delete anything,
  because every deletion still needs your approval;
- the web page listens on 127.0.0.1 only and rejects requests from other sites.

## How good is it?

**Measured once on a real model (llama3.1:8b, the author's Windows PC), before the 0.3 permission model:** 20/20 tasks
of the then-20-task suite on the final run; earlier runs scored 18/20 and 19/20 with different tasks failing, so expect
run-to-run variation. **0.3 changed the tools and the prompt, so those numbers no longer describe this version.** The
suite now has 44 tasks (including *"What are the files present in this folder? Present them as file type and then the total
count"*, and refusing to read a file you did not allow) and none of the 0.3 run has been done on a real model yet. Other
models are unmeasured. The 360 tests exercise everything except the model's own judgement. To measure on your machine:

```
python -m evals.run --model llama3.1:8b
```

It checks the resulting files and the facts in the answer, not the model's wording. Results go to `evals/results/`.
`python -m evals.run --selftest` verifies the tasks themselves without a model. The PDF tasks are skipped unless `pypdf` (and, for the formatting task, `pdfminer.six`) is installed.

## Develop

```
make install && make check      # ruff, mypy --strict, 360 tests with a 90% coverage floor, eval self-test
```
Layout: `hearth/access.py` (who may do what, anywhere), `filetypes.py` (file categories), `tools.py` (20 actions) and `tools_more.py` (15 more, 35 in all), `pdfrich.py` (PDF look into Word), `pdfout.py` (Word/text into PDF) and
`tools_base.py` (permission prompts, safe folder walking), `sheets.py` / `docxio.py` (spreadsheet and Word editing),
`finder.py` (search inside files), `agent.py` (the loop), `index.py` (search index), `journal.py` (undo),
`model.py` (local-only Ollama client), `cli.py`, `webui.py`.
