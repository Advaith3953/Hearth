"""The conversation from the screenshots: a job-application sheet, 'what are the values under Company', and a model that
wandered off and proposed an edit nobody asked for."""
import json
from pathlib import Path

import openpyxl
import pytest
from conftest import Asker

from hearth.agent import Agent, wants_change
from hearth.model import ScriptedModel
from hearth.tools import Ctx, run_tool


def tool(name: str, **args) -> str:
    return json.dumps({"tool": name, "args": args})


def answer(text: str) -> str:
    return json.dumps({"answer": text})


@pytest.fixture
def jobs(root: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Sl.No", "Company Name", "Position applied", "Date of submission", "Application status"])
    ws.append([1, "Amazon", "Data Engineer intern", "2026-11-08", "Pending"])
    ws.append([2, "Zoho", "Analyst", "2026-11-09", "Pending"])
    ws.append([3, "Amazon", "UX intern", "2026-11-10", ""])
    ws.append([4, None, None, None, None])
    p = root / "New Microsoft Excel Worksheet.xlsx"
    wb.save(p)
    return p


def run(ctx: Ctx, name: str, **args):
    return run_tool(ctx, name, args)


# ---- bug 1: there was no way to read the values under a column

def test_values_under_a_partial_column_name(ctx: Ctx, jobs: Path):
    out = run(ctx, "sheet_column", path=str(jobs), column="Company")
    assert "Column 'Company Name' (B)" in out
    assert "row 2: Amazon" in out and "row 3: Zoho" in out and "row 4: Amazon" in out
    assert "3 value(s)" in out


def test_unique_values_count_and_empty_cells(ctx: Ctx, jobs: Path):
    out = run(ctx, "sheet_column", path=str(jobs), column="Company Name", unique=True)
    assert "2 different value(s)" in out and "Amazon  (x2)" in out and "Zoho" in out and "row 2" not in out
    status = run(ctx, "sheet_column", path=str(jobs), column="status")
    assert "2 value(s), 2 empty" in status        # rows 4 and 5 have other cells but no status


def test_column_limit_letter_missing_column_and_selected_file(ctx: Ctx, jobs: Path):
    out = run(ctx, "sheet_column", path=str(jobs), column="A", limit=2)
    assert "4 value(s)" in out and "2 more" in out and "row 2: 1" in out and "row 4" not in out
    err = run(ctx, "sheet_column", path=str(jobs), column="Salary")
    assert err.startswith("ERROR") and "Company Name" in err
    ctx.selected = str(jobs)
    assert "Amazon" in run(ctx, "sheet_column", column="Company Name")  # no path: the selected file


def test_title_row_above_the_headers_and_second_sheet(ctx: Ctx, root: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cover"
    ws.append(["Applications 2026"])
    ws2 = wb.create_sheet("Data")
    ws2.append(["Report"])
    ws2.append(["Company Name", "City"])
    ws2.append(["Zoho", "Chennai"])
    p = root / "t.xlsx"
    wb.save(p)
    out = run(ctx, "sheet_column", path=str(p), column="Company")
    assert "sheet 'Data'" in out and "Zoho" in out
    assert "no column" in run(ctx, "sheet_column", path=str(p), column="Company", sheet="Cover")


def test_searching_for_a_column_name_as_if_it_were_a_value_gets_a_hint(ctx: Ctx, jobs: Path):
    out = run(ctx, "sheet_find", path=str(jobs), text="Company", column="Company Name")
    assert out.startswith("No row contains") and "sheet_column" in out
    assert "sheet_column" not in run(ctx, "sheet_find", path=str(jobs), text="Nonexistent", column="Company Name")


def test_a_full_conversation_about_the_column_is_answered(ctx: Ctx, jobs: Path):
    m = ScriptedModel([tool("sheet_column", column="Company Name", unique=True),
                       answer("The companies are Amazon and Zoho.")])
    ctx.selected = str(jobs)
    res = Agent(m, ctx).run('what are the values under "Company"')
    assert res.ok and "Amazon" in res.answer


# ---- bug 2: a question must never lead to a proposed edit

@pytest.mark.parametrize("text", [
    "What are the values under Company", 'under "Company Name" then?', "what do you mean repeating yourself?",
    "What excel file has the column Company name? search all drives", "how many files are here", "list my pdfs", "hi",
])
def test_questions_are_not_change_requests(text: str):
    assert not wants_change(text)


@pytest.mark.parametrize("text", [
    "set the status of Amazon to Rejected", "change E2 to Rejected", "create a new word document", "copy the Date column",
    "Delete old.txt", "mark Zoho as Selected", "paste it in E:\\name.word", "rename it", "move it to Downloads",
    "append a line", "Open the file", "fix the typo", "update the status",
])
def test_change_requests_are_recognised(text: str):
    assert wants_change(text)


def test_a_yes_only_counts_after_a_change_request():
    asked = [{"role": "user", "content": "change Amazon to Rejected"}, {"role": "assistant", "content": "{}"}]
    assert wants_change("yes, go ahead", asked)
    answered = [{"role": "user", "content": "what are the values under Company"}, {"role": "assistant", "content": "{}"}]
    assert not wants_change("yes", answered) and not wants_change("yes")


def test_the_edit_from_the_screenshot_is_blocked_and_nothing_is_asked(ctx: Ctx, jobs: Path, asker: Asker):
    """'what do you mean repeating yourself?' -> the model called sheet_set_cell on Amazon's status."""
    ctx.selected = str(jobs)
    before = jobs.read_bytes()
    m = ScriptedModel([tool("sheet_find", text="Amazon"),
                       tool("sheet_set_cell", column="Application status", value="Rejected", expect="Pending"),
                       answer("I did not change anything; I stopped because I was repeating the same search.")])
    res = Agent(m, ctx).run("what do you mean repeating yourself?")
    assert res.ok and "did not change" in res.answer
    assert asker.asked == [] and jobs.read_bytes() == before     # not even a question: the user was never bothered
    assert any("no change is allowed" in c["content"] for c in m.calls[-1] if c["role"] == "user")


def test_every_change_tool_is_blocked_on_a_question(ctx: Ctx, jobs: Path, root: Path, asker: Asker):
    ctx.allow_changes = False
    calls = {"write_file": dict(path=str(root / "n.txt"), content="x"), "make_folder": dict(path=str(root / "d")),
             "delete_path": dict(path=str(jobs)), "move_path": dict(source=str(jobs), destination=str(root / "z.xlsx")),
             "copy_path": dict(source=str(jobs), destination=str(root / "c.xlsx")), "open_file": dict(path=str(jobs)),
             "append_text": dict(path=str(root / "n.txt"), text="x"),
             "replace_text": dict(path=str(root / "n.txt"), old="a", new="b"),
             "copy_to_word": dict(destination=str(root / "w.docx"), text="x"),
             "sheet_set_cell": dict(path=str(jobs), column="Company Name", value="x", row=2)}
    for name, args in calls.items():
        assert "no change is allowed" in run(ctx, name, **args), name
    assert not (root / "n.txt").exists() and not (root / "d").exists() and jobs.exists() and asker.asked == []
    assert "Amazon" in run(ctx, "sheet_column", path=str(jobs), column="Company")    # reading still works


def test_a_real_change_request_still_goes_through_the_normal_question(ctx: Ctx, jobs: Path, asker: Asker):
    ctx.selected = str(jobs)
    m = ScriptedModel([tool("sheet_find", text="Zoho", column="Company Name"),
                       tool("sheet_set_cell", column="Application status", value="Rejected", expect="Pending"),
                       answer("Done.")])
    res = Agent(m, ctx).run("set the status of Zoho to Rejected")
    assert res.ok and len(asker.asked) == 1
    assert openpyxl.load_workbook(jobs).active["E3"].value == "Rejected"


# ---- bug 3: the 'I keep repeating myself' message dumped raw JSON

def test_the_stop_message_is_plain_words_and_honest_about_changes(ctx: Ctx, jobs: Path):
    ctx.selected = str(jobs)
    same = tool("sheet_find", text="Company", column="Company Name", path="D:\\Check\\Hearth\\x.xlsx")
    res = Agent(ScriptedModel([same, same, same]), ctx).run("under Company Name then?")
    assert not res.ok
    assert "stuck repeating" in res.answer and "I did not change anything" in res.answer
    assert "{" not in res.answer and "\\\\" not in res.answer and "sheet_find" not in res.answer


def test_the_summary_admits_a_change_that_was_made(ctx: Ctx, jobs: Path):
    ctx.selected = str(jobs)
    find = tool("sheet_find", text="Zoho", column="Company Name")
    res = Agent(ScriptedModel([find, tool("sheet_set_cell", column="Application status", value="Rejected"), find, find, find]),
                ctx).run("set Zoho to Rejected")
    assert not res.ok and "Something was changed" in res.answer and "did not change anything" not in res.answer
