# Security notes

Hearth gives a language model the ability to look at, and act on, the files on your computer. The design assumes the
model is unreliable and that file contents may be hostile.

## The access model (0.3)

| Action | Rule |
|---|---|
| **List** files/folders anywhere: names, types, sizes, dates, counts | Free. No content is opened. |
| **Read** content (`read_file`, `search_text`, spreadsheet tools, editing tools) | Allowed without asking only inside *trusted folders* (`hearth grant`) and places you allowed earlier in this run. Anywhere else the user is asked: *Allow this file* / *Allow this folder until Hearth closes* / *Don't allow*. Allowances are kept in memory only. |
| **Change** (create, edit, move, copy, delete, open) | Only when your message asks for a change (a plain question cannot trigger one; checked in code), and then it always asks, one change at a time. `--auto-safe` skips the question for *new* files/folders and moves/copies **only when every path involved is inside a trusted folder**. |
| Secret stores: `.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube`, browser profiles (Chrome, Edge, Firefox, Brave), Windows credential/vault folders, `*.kdbx`, `*.pfx`, `id_rsa`, `.netrc`, ... | Never listed, read or changed, **even if the user says yes**. |
| Hearth's own data folder | Never opened. |
| Operating-system folders (Windows, Program Files, `/usr`, `/etc`, ...) | Read with permission; never changed, never deleted. |
| Drives, your home folder, Desktop/Documents/Downloads/..., trusted folders | Cannot be deleted or moved themselves. |

## What is enforced in code (and tested)

| Risk | Control |
|---|---|
| Reading files you did not allow | `Access.resolve` on every path (`..` and symlinks resolved first), then `need_read` before any content is read. A denial returns `DENIED` to the model and opens nothing. Tested. |
| Secrets being handed over by a mistaken or injected "yes" | The secret-store list is checked *before* any question is asked, so there is no prompt to click through. |
| Scanning a whole disk hanging or looping | Walks skip hidden folders, `AppData`, `node_modules`, system folders, links and Windows junctions, and stop at a time/file limit (the result says it is partial). |
| A file or web text telling the model to do something harmful (prompt injection) | File text is returned fenced as data, and the system prompt says to ignore instructions in it. The hard guarantees are different: a message that is only a question cannot run any change tool (a word list in code, so an unusual phrasing of a real request may be refused), and **every change needs your approval** and every new read outside trusted folders needs your permission, so an obeyed injection becomes a visible prompt you can refuse. Tested with a hostile file and a model that obeys it. |
| Mistakes by the model | Deleting moves to a trash; overwrites are backed up; moves and copies are journalled; `undo` reverses them. Edits require an exact, unique match. Text files, `.docx` text, and single spreadsheet cells can be modified; PDFs and images cannot. |
| A wrong cell or row being changed in a spreadsheet | `sheet_set_cell` changes exactly one cell; it refuses formulas and macro-enabled workbooks, checks the value the model says is there (`expect`), asks which row if several match, and shows `old -> new` on the approval card. The previous file is backed up. |
| Launching programs | `open_file` has an allowlist of document/media types, refuses executables and scripts, and always asks. |
| Internet access | The model client accepts only `http://127.0.0.1`, `localhost` or `[::1]` and ignores proxy settings. No tool performs network I/O. |
| Other websites or local programs driving the web page | 127.0.0.1 only; Host and Origin checks; a custom header required on every POST; request size limits. |

## What is NOT protected against

- **Listing is deliberately broad.** Hearth can tell the model the names, sizes and dates of files anywhere you point it
  (a file name can itself be private), and those names are part of the chat window and the model's context.
  Only secret-store locations are hidden.
- **The secret-store list is best effort.** It covers common locations, not every password manager, token file or
  application's private data. Do not rely on it as the only protection for a sensitive file; if in doubt, answer *Don't allow*.
- **"Allow this folder"** covers everything under that folder, including subfolders you did not look at, until Hearth closes.
- **Selecting a file in the web page** counts as allowing Hearth to read it (and the page shows a preview of it).
- **Approval fatigue.** If you click Allow without reading, nothing stops a bad read or change except undo (for changes).
- **Not a network firewall.** "Offline" is enforced inside Hearth, not by the operating system. Ollama itself, other
  programs, and your OS are outside this guarantee.
- **Information in the model's reply.** Anything you allow Hearth to read can end up in the chat window. It stays on this
  computer, but treat the window like the files themselves.
- **Same-user processes.** Anything running as you can read Hearth's data folder and the local page.
- **Spreadsheet fidelity.** Excel files are edited by patching one cell in the sheet's XML and leaving every other part of the file as it was. This was verified by reading the results back with an independent library (openpyxl) and with hand-built files, **not with desktop Excel, LibreOffice or Google Sheets**. If the workbook has formulas, Hearth marks it to recalculate when opened, so Excel may ask to save on close, and other programs may show old formula results until then. A text cell is rewritten as an inline string. Keep a copy of important workbooks until you have tried it.
- **Untrusted documents.** Spreadsheets, Word files and PDFs are parsed with Python's built-in XML parser (and `pypdf` and `pdfminer.six` for PDFs). Size limits apply, but a deliberately crafted file could still make parsing slow or fail.
- **Model quality.** A small model can misread a request, or skip asking you by choosing a different tool. The permission checks live in the tools, not in the prompt, so they cannot be skipped; but the model's *choice* of what to look at is only as good as the model.
- **Not tested on macOS or Windows yet** for the 0.3 access rules (Windows junction and system-folder handling were written from documentation, not run on Windows). No independent security review.

## Added in 0.4 (bulk and file-handling tools)

- Finding duplicates reads file contents (a hash), so it asks to read the folder first, like any read.
- Tidy, bulk rename and move-duplicates show a plan and ask **once**; the whole batch is one entry in the undo list and is undone as one.
  They never delete or overwrite; a name that already exists is skipped, hidden files, links and blocked places are left alone.
- Unpacking a `.zip` always goes into a new folder and refuses paths that climb out of it, absolute paths, links, and archives that expand
  too large; a failed unpack is cleaned up.
- Merging, extracting, filling a template and making a spreadsheet write a **new** file; the originals are never touched.
- Finding deadlines reads document text only with permission, and is pattern-based: it can miss a date or flag a wrong one.
- Approval dialogs for these tools are tested with a stand-in, not the real web page. Not tested on Windows drives or junctions.

## Reporting

Open an issue with steps to reproduce. Do not include private files.
