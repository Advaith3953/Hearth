"""The conversation where nothing was created: the file was in Downloads, the follow-ups ('check again', 'it does exist',
'all changes are allowed') had no change verb, so the change gate refused them, and the model then claimed success."""
import json
from pathlib import Path

import pytest
from conftest import Asker
from reportlab.pdfgen import canvas

from hearth.access import AccessError
from hearth.agent import Agent, wants_change
from hearth.model import ScriptedModel
from hearth.tools import Ctx
from hearth.tools_base import ALLOW_FILE

REQUEST = ('from Downloads copy the contents, formats, etc. from R_Advaith_Resume.pdf and create a new word document in E: Drive '
           'named "second run" and then paste the contents and the exact formatting in that document')


def tool(tool_name: str, /, **args) -> str:
    return json.dumps({"tool": tool_name, "args": args})


def answer(text: str) -> str:
    return json.dumps({"answer": text})


@pytest.fixture
def pc(tmp_path: Path, ctx: Ctx) -> Path:
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Desktop").mkdir()
    (tmp_path / "E").mkdir()
    ctx.access.home = str(home)
    c = canvas.Canvas(str(home / "Downloads" / "R_Advaith_Resume.pdf"))
    c.drawString(72, 720, "R. ADVAITH resume")
    c.save()
    return tmp_path


def copy_call(pc: Path, source: str = "R_Advaith_Resume.pdf") -> str:
    return tool("copy_to_word", source=source, destination=str(pc / "E"), name="second run")


# ---- a file that is "in Downloads" is found even when the model writes a bare name or the wrong drive

@pytest.mark.parametrize("written", ["R_Advaith_Resume.pdf", "Downloads/R_Advaith_Resume.pdf", "E:\\Downloads\\R_Advaith_Resume.pdf",
                                     "Downloads\\R_Advaith_Resume.pdf", '"R_Advaith_Resume.pdf"'])
def test_a_named_file_is_found_in_the_usual_places(pc: Path, ctx: Ctx, written: str):
    assert ctx.access.resolve(written) == (pc / "home" / "Downloads" / "R_Advaith_Resume.pdf").resolve()


def test_a_missing_name_still_fails_and_two_matches_are_not_guessed(pc: Path, ctx: Ctx):
    with pytest.raises(AccessError, match="does not exist"):
        ctx.access.resolve("nothing-called-this.pdf")
    (pc / "home" / "Desktop" / "R_Advaith_Resume.pdf").write_bytes(b"x")
    with pytest.raises(AccessError, match="several files match"):
        ctx.access.resolve("R_Advaith_Resume.pdf")
    assert ctx.access.resolve("Downloads/R_Advaith_Resume.pdf").parent.name == "Downloads"      # an exact path still wins


# ---- the retries

def test_the_users_actual_follow_ups_count_as_change_requests():
    assert wants_change(REQUEST)
    assert wants_change("for this required operation, all changes are allowed")
    assert not wants_change("check again") and not wants_change("it does exist.")       # these rely on the pending change below


def test_the_whole_conversation_ends_with_the_file_created(pc: Path, ctx: Ctx, asker: Asker):
    asker.choice = ALLOW_FILE
    m = ScriptedModel([answer("The file does not exist."),                         # turn 1: the model gives up
                       answer("Still cannot find it."),                           # turn 2: "check again"
                       copy_call(pc), answer("Created second run.docx in E:.")])  # turn 3: "it does exist." -> now it works
    a = Agent(m, ctx)
    assert "does not exist" in a.run(REQUEST).answer
    assert a.run("check again").ok
    res = a.run("it does exist.")
    assert "Created" in res.answer and "Nothing was changed" not in res.answer
    assert (pc / "E" / "second run.docx").exists()


def test_a_retry_is_not_blocked_but_a_later_question_is(pc: Path, ctx: Ctx, asker: Asker):
    asker.choice = ALLOW_FILE
    a = Agent(ScriptedModel([answer("could not"), copy_call(pc), answer("done"),
                             tool("write_file", path="x.txt", content="no"), answer("I tried.")]), ctx)
    a.run(REQUEST)
    assert a.run("try again").answer == "done" and (pc / "E" / "second run.docx").exists()
    n = len(asker.asked)
    res = a.run("what do you mean?")                           # the change happened, so a plain question is just a question
    assert len(asker.asked) == n and not (ctx.access.trusted and (Path(ctx.access.trusted[0]) / "x.txt").exists())
    assert res.answer.endswith("Nothing was changed.")        # and a made-up 'done' can never stand


def test_a_fresh_question_ends_the_pending_change(pc: Path, ctx: Ctx, asker: Asker):
    a = Agent(ScriptedModel([answer("not found"), tool("make_folder", path=str(pc / "E" / "sneaky")), answer("I made it.")]), ctx)
    a.run(REQUEST)
    res = a.run("which files are in Downloads?")
    assert not (pc / "E" / "sneaky").exists() and asker.asked == [] and res.answer.endswith("Nothing was changed.")


def test_pending_change_expires_after_three_retries(pc: Path, ctx: Ctx):
    replies = [answer("no")] + [answer("still no")] * 3 + [tool("make_folder", path=str(pc / "E" / "late")), answer("ok")]
    a = Agent(ScriptedModel(replies), ctx)
    for text in (REQUEST, "check again", "once more", "and again"):
        a.run(text)
    assert a.pending_turns == 0
    a.run("hello there")
    assert not (pc / "E" / "late").exists()


def test_the_end_to_end_request_with_a_bare_file_name(pc: Path, ctx: Ctx, asker: Asker):
    asker.choice = ALLOW_FILE
    res = Agent(ScriptedModel([copy_call(pc), answer("Created second run.docx.")]), ctx).run(REQUEST)
    assert res.ok and (pc / "E" / "second run.docx").exists()
