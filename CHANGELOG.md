# Changelog

## 0.4.2
From a real run: "make 8 PDF copies of second run in a new folder Checking" produced no PDF at all. The 8B model used `copy_to_word`
(Word, not PDF), then `pdf_merge`, then `append_text`, and finally said it had made 8 PDFs. Eight separate correct calls were too much to ask.
- `save_as_pdf` takes `copies=N`: one call, one question, N PDFs named `<name> 1.pdf` ... in a folder (made if new). Numbers already
  taken are skipped, nothing is overwritten, one undo takes back the folder and all the files. Up to 50.
- Hearth reads the count from the request ("8 pdfs", "eight copies") and fills it in itself, so the model cannot forget it, and ignores
  extra `save_as_pdf` calls once the copies exist.
- If the request asks for PDFs and the model reaches for `copy_to_word`, `append_text` or `write_file`, the call is refused with the instruction to use
  `save_as_pdf`. (Not done when the request says "into Word".)
- "copies" now counts as a change request, so "no I see no copies" is not refused as a question.
- A repeated step now tells the model to move on to the next step instead of "try something different".
- New eval task: eight-pdf-copies-in-a-new-folder (44 tasks). Not tested: the real model on any of this.

## 0.4.1
From a real run: "make 8 PDF copies of third run" asked eight separate times.
- Questions about **new files, copies and moves** now have a second button, **"Allow all new files in this request"**. One click covers the rest
  of that one request (all eight PDFs); it ends when the request ends. Replacing, editing, deleting and opening still ask every time, and so
  does a new request. The plain **Allow** still approves just one. The command line offers the same choice as option 2.
- Not tested: the button in the real web page (the page already drew any number of buttons; tested with a stand-in click).

## 0.4.0
Fifteen new tools (35 in all, in `tools_more.py`): compare_files, find_duplicates, move_duplicates, tidy_folder, bulk_rename, pdf_merge,
pdf_extract, images_to_pdf, fill_template, create_spreadsheet (a real `.xlsx` writer, stdlib only), zip_contents, extract_zip, show_history,
undo_change, find_deadlines. Changes that touch many files ask once and are one undo step (new `batch` entry in the undo journal).
- 354 tests at 95% coverage, 43 eval tasks (11 new). Extras: `pypdf`, `pillow`, `pdfplumber` (`pip install .[more]`).
- Not tested: the real 8B model with 35 tools in its prompt (a longer prompt may make a small model pick the wrong tool; unmeasured), Word's
  own PDF export, the new approval dialogs in the real web page, Windows drives/junctions.
- Limits: fill_template does not fill headers/footers; PDF tables only if ruled (pdfplumber); images_to_pdf needs Pillow; only `.zip`
  (no 7z/rar/tar); deadlines are regex-based (dd/mm/yyyy read day-first); the built-in PDF writer is approximate.

## 0.3.6
From a real run: the copy of a one-page PDF was about 80% right, came out as two pages, and lost the links, the lines, font sizes and colours,
and line spacing. Checked against the real resume PDF, rendered through LibreOffice, line by line: one page, and no line more than 0.8 pt away
from where it sits in the PDF (it was 3.9 pt and growing).
- Links: the resume's web addresses are only underlined text, with no link inside, so there was nothing to copy. Underlined web addresses and
  anything that looks like a link or an e-mail address now become real links (the underline is kept only where the PDF had one).
- Lines: straight rules (under headings, under the header) become a bottom border in the paragraph's colour and thickness.
- Spacing: the page's own distance between lines is set exactly, and the space above each paragraph is worked out from where Word will really
  put the previous line, so rounding does not pile up down the page.
- Page count: fonts Word does not have but that have an exact look-alike are swapped (Carlito to Calibri, Liberation Sans to Arial, ...); the top
  and bottom margins allow for the exact line height. Fixes the spill onto a second page. A font that is not on the computer and has no
  look-alike is still replaced by Word, and then lines can break differently.
- 319 tests. Not tested: opening the result in desktop Word (checked in LibreOffice and by reading the file), other PDFs than the resume-like
  test pages. The built-in PDF writer (Word to PDF without Word or LibreOffice) does not draw the rules and ignores the exact spacing.

## 0.3.5
From a real conversation: "now from 'second run' convert it back into a pdf ... under the name 'third check'". Hearth said 'second run'
did not exist, and in the end made `third check.docx`, not a PDF, while claiming it had converted.
- New `save_as_pdf`: Word and text files (.docx .txt .md .csv .json ...) become a PDF. Engines in order, and the answer says which one
  was used: Microsoft Word (Windows, through PowerShell; **not tested on a real Word**), LibreOffice (tested), then a built-in writer
  (needs `pip install reportlab`; close but not identical: standard fonts only, non-Latin letters become "?", tables become text lines).
  The old tool could only make Word files, which is why the output was always .docx.
- Names without an extension: "second run", "resume", "third check" find `second run.docx` etc. by name, first in the folder the path names,
  then in the home folder, Desktop, Documents, Downloads, trusted folders and the drives. If a .docx and a .pdf share the name, Hearth uses
  the kind that fits the job, otherwise it lists the candidates and asks. The model is told never to add or change an extension itself.
- A made-up "done": if the answer says a file was created or converted and no change happened, "Nothing was changed." is added.
- 310 tests and 32 eval tasks (new: word-name-without-extension-into-pdf). Not tested: the real 8B model on this request, Word's own
  export, Windows drives.

## 0.3.4
From a real conversation where no file was created. The request was "from Downloads copy ... R_Advaith_Resume.pdf and create a new word
document in E: Drive named second run".
- Found and fixed (my 0.3.2 change gate caused this): the retries "check again", "it does exist." and "for this required operation, all
  changes are allowed" contain no change verb, so every change was refused and the user was stuck; the model then claimed the file was
  copied. Now a change the user asked for stays allowed for up to three follow-up turns until it happens, and a new question
  ("which files ...?") ends that. When a change was refused or denied and nothing changed, Hearth adds "Nothing was changed." to the
  answer itself, so a made-up "done" cannot stand.
- Found and fixed: "R_Advaith_Resume.pdf from Downloads" was reported as not existing when the model wrote the bare name, or the wrong
  drive (`E:\Downloads\...`). If the path does not exist, a file with that name sitting directly in the home folder, Desktop, Documents,
  Downloads or a trusted folder is used when there is exactly one; with two it asks for the full path. Reading it still asks permission.
- 297 tests. Untested: the real 8B model on this request.

## 0.3.3
From a real conversation: "copy the contents, formats, etc. from R_Advaith_Resume.pdf and create a new word document in E: Drive named
'second run'". It worked, but the formatting and links were lost, the file was not called "second run", and the answer said "E:older".
- PDF to Word now keeps the look (new module `pdfrich.py`, needs `pip install pdfminer.six`): fonts, sizes, bold/italic, colours,
  centred/right alignment, indents, space between paragraphs, bullets (including the dots Chrome and Word draw as shapes), dates on the
  right of a line, page breaks, page size and margins, and hyperlinks (web and email). A PDF has no real paragraphs, so they are rebuilt
  from where the letters sit. Checked by converting a real one-page resume and rendering both in LibreOffice: it came out on one page, close
  to the original. Not copied: images, drawn lines/shapes/backgrounds (e.g. the rule under a heading), real tables; fonts that are not
  installed are replaced by Word; white text is turned black because the page background is not copied.
- A Word file copied to a new name is now an exact copy (everything kept).
- New `name` argument for `copy_to_word`: with a folder or drive as destination the new file is called what the user said. `plain=true` asks
  for text only. Without pdfminer.six, for a PDF that cannot be laid out, or when adding to an existing document, it falls back to plain text and
  says so in the approval card and the result.
- The plain-text fallback is now unmistakable: the result says the formatting and links were NOT kept and gives the exact command
  (`python -m pip install pdfminer.six`), so the model no longer words it as if they were.
- Found and fixed: a Windows path in an answer (`E:\folder`, `D:\temp\new`) lost its backslash to JSON escapes (`\f` became a form feed, so
  "E:older"). Answers are repaired like arguments already were.
- Eval suite: 31 tasks (1 new: named Word document from a PDF). Not yet run on a real model.
- Untested: desktop Word (checked with python-docx and LibreOffice only), scanned PDFs (no text, so plain fallback), PDFs with several columns or
  real tables, right-to-left or non-Latin scripts, and the real 8B model on this request.

## 0.3.2
From a real conversation (a job-application sheet): "What are the values under Company" failed, and "what do you mean repeating
yourself?" made Hearth propose changing a cell to "Rejected" that nobody asked about.
- New tool `sheet_column`: lists the values under a column. The name may be partial ("Company" finds "Company Name"), a title row above
  the headers is handled, `unique=true` gives each different value once with counts. Before this the only option was `sheet_find`, which
  searches inside cells, so the model searched for the word "Company" and got nowhere. `sheet_find` now says so when the text it was given
  is a column header.
- Found and fixed: a question could lead to a proposed edit. Whether the user asked for a change is now decided in code
  (`wants_change`): when the message is a question, every change tool is refused before it runs, so the user is not even asked. A "yes" only
  counts after a change request. Change requests (set, change, create, copy, delete, ...) work as before, including the approval question.
  This is a word list, so an unusual phrasing of a real change request may be refused; the answer then tells the user to say it as a change.
- Found and fixed: the "I keep repeating myself" message printed raw tool JSON with doubled backslashes. It is now plain words, and it only
  says nothing was changed when that is true.
- Eval suite: 30 tasks (2 new: values under a column, a question must not edit). Not yet run on a real model.
- Untested: the real 8B model on these requests; whether the word list misses phrasings you actually use.

## 0.3.1
Three-pass bug hunt using these requests: "get me an excel sheet from D:\\folder1\\folder2"; "copy only the Date column from
D:\\folder1\\name.xlsx and paste it in E:\\name.word"; "create a new word document, copy pages 1-23 of E:\\...\\name.pdf and save it in Downloads".
- New tool `copy_to_word`: creates a .docx (or adds to one) from a spreadsheet column, PDF pages (`1-23`, `2,5`), a text/Word file or
  typed text. Hearth does the copying itself, so long content is not retyped by the model. `.word`/`.doc`/no extension become `.docx`; a
  folder destination (Downloads) gets a file named after the source; reading the source asks permission, the write asks approval.
- Found and fixed: Windows paths written with single backslashes in the model's JSON (`D:\folder1\folder2`) were corrupted (`\f` is a form
  feed, `\n` a newline) or made the reply invalid JSON, so folders were "not found". Invalid escapes are now repaired, control characters in path
  arguments are turned back into backslashes, and the prompt asks for doubled backslashes.
- Found and fixed: `Downloads/new.docx` was created inside the trusted folder instead of the Downloads folder.
- Found and fixed: a column was only looked for in the first row; sheets with a title above the headers now work.
- Found and fixed: the "source is a folder" check came after the permission question.
- PDF reading limit raised from 300 to 2000 pages. `"E:"` now means the drive, not the current folder on E.
- Eval suite: 28 tasks (3 new: named folder, column into Word, PDF page into Word). Not yet run on a real model.
- Untested: Windows drive letters and junctions on a real Windows machine; the real 8B model on these requests; Word files opened in desktop Word
  (checked with python-docx only).

## 0.3.0
Redesign: Hearth now works with files of every type anywhere on the computer, and asks permission instead of being
limited to shared folders. Prompted by a real run in which "What are the files present in this folder? Present them as
file type and then the total count" made the model search only for Excel files, three times.
- **Access model** (`hearth/access.py`, replaces `sandbox.py`): listing anywhere is free (names, sizes, dates, counts);
  reading content asks (*Allow this file* / *Allow this folder until Hearth closes* / *Don't allow*) unless the place is a
  trusted folder (`hearth grant`, formerly "share") or was allowed earlier in the run; every change still asks.
  Secret stores and Hearth's own data are never opened, system folders are read-only, drives/home/trusted folders cannot be
  deleted or moved. `--auto-safe` now applies only inside trusted folders.
- **Tools**: new `folder_summary` (file counts by type, then the total); `find_files` takes a name and/or a type
  (documents, spreadsheets, PDFs, presentations, text, code, images, audio, video, archives, programs) and works anywhere;
  `search_text` covers content, spreadsheet column names and file names, takes `kind` and `folder`, and asks before reading a
  new folder; `find_documents` is gone (merged into those two). `list_folder` with no path shows the chosen folder or the usual places.
  Whole-disk scans have time and size limits and skip hidden/noisy/system folders and links.
- **Prompt**: rewritten for general files, with placeholder examples only; it tells the model that listing is free, reading is asked, and
  what to do when told DENIED.
- **Web page / CLI**: permission questions have one button per choice (terminal: numbered); the page works with no trusted
  folder; selecting a file in the page allows reading it; previews of files you have not allowed are withheld.
- Fixed while testing: the secret-store check was case-sensitive on macOS/Linux (a `Google/Chrome/User Data` path was not blocked).
- Verification: 212 tests, 96% line coverage, ruff and mypy --strict clean, eval self-test clean (25 tasks; 5 new, incl. the request above),
  headless-browser check of the permission card.
- **Not done:** the 25-task suite has not been run on a real model since this redesign; nothing run on macOS, or on Windows
  since the change; other models; desktop-Excel check of edited workbooks; PDF editing; `.xls`/`.doc`/`.pptx`.

## 0.2.0
Find, open and change spreadsheets, Word files and PDFs from the web page.
- New tools: `find_documents` (Excel files are matched by column header, then cell text; Word/PDF/text by content),
  `sheet_info`, `sheet_find`, `sheet_set_cell`; `search_text` accepts a file kind; `replace_text` / `append_text` now work on `.docx`.
- Web page: Find bar with type chips, file cards that say why each file matched, Select, a "Working on" bar, a live preview
  (spreadsheet table with the focused rows highlighted, or text), and plain-sentence progress lines instead of raw JSON.
- "That row / that file" follow-ups: the assistant is told what is selected and which rows were last found.
- Reads `.xlsx`, `.csv`/`.tsv` and `.docx` without extra packages; reads PDF with the optional `pypdf`.
- Fixed (found in a real run): the model could not read `.xlsx`, wandered into the program's own files, and called a
  non-existent `answer` tool. Reading now works; an `answer`/`final_answer` "tool" is understood as finishing.
- `find_documents` now ends by telling the model to name every file found (only for that tool: applying it to `search_text` made the model list weak matches), and the prompt forbids asking the user to "select" a file (a real 8B run did this).
- The file `kind` the model passes is now understood loosely ("Excel files", "xlsx", "spreadsheets") instead of being rejected.
- Search: a query with several words now returns files containing all of them, and falls back to "any word" only when none does (a real 8B run listed weak matches as answers).
- Fixed (found in a real run): asking "what are the files in the current folder?" made the model search for Excel files with a 'Country' column, because that literal example was in the prompt and in a tool description. Examples are now placeholders, the prompt explains there is no "current folder", and `list_folder` with no path lists the folder itself when only one is shared. New eval task `current-folder` (not yet run on a real model).
- Fixed (found in a real run): "what are the excel files in this folder?" found nothing, because `find_documents` required a search word. A kind on its own now lists every file of that kind (newest first), with an optional `folder`. The model had searched for the folder's name instead. Sharing a folder and one inside it no longer searches the inner one twice, and with nested shares `list_folder` lists the outer folder. New eval task `list-excel-in-folder` (not yet run on a real model).
- Fixed (found in a real run): progress lines showed placeholder dots ("Looking inside ...") when optional arguments were left out; they now read as sentences. The prompt tells the model to ask what the user wants, not repeat its last answer, when a request is unclear or a stray character.
- Eval suite: 22 tasks (9 new) (7 new), with binary fixtures, a pre-selected file and multi-turn tasks.
- Verification: 165 tests, 95% line coverage, ruff and mypy --strict clean, eval self-test clean.
- Measured: one run of the 20-task suite on llama3.1:8b (Windows) passed 20/20; earlier development runs scored 18-19/20.
- Not done: other models; macOS runs; checking edited workbooks in desktop Excel; PDF editing; `.xls`/`.doc`/`.pptx`.

## 0.1.0
First release: permission-scoped sandbox, 13 file tools (search, read, create, edit, move, copy, delete-to-trash, open),
undo journal, SQLite FTS5 search index, JSON-action agent loop for ~8B models, loopback-only Ollama client, CLI,
local web page with approval cards, eval suite (13 end-state tasks + self-test).
Verification: 104 tests, 96% line coverage, ruff and mypy --strict clean.
Not yet done: measurement on a real 8B model, macOS/Windows runs, embeddings-based search, PDF reading.
