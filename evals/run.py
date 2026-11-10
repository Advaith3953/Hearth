"""Run the eval tasks against a real local model (needs Ollama), or --selftest to validate the tasks themselves.

    python -m evals.run --model llama3.1:8b            # real evaluation; writes evals/results/<model>.json
    python -m evals.run --selftest                     # no model: scripted perfect runs + a do-nothing run per task
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from hearth.agent import Agent, parse_action, remember
from hearth.app import make_ctx
from hearth.config import Config
from hearth.model import Model, ModelError, OllamaModel, ScriptedModel

from .tasks import TASKS, A, Task, available


class Counting:
    """Wraps a model to count calls and replies that were not usable actions."""

    def __init__(self, inner: Model) -> None:
        self.inner = inner
        self.calls = 0
        self.unusable = 0

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        out = self.inner.chat(messages)
        if parse_action(out) is None:
            self.unusable += 1
        return out


def run_task(task: Task, model: Model, max_steps: int = 8) -> dict[str, object]:
    work = Path(tempfile.mkdtemp(prefix="hearth-eval-"))
    root, home = work / "shared", work / "home"
    for name, text in task.outside.items():
        (work / "elsewhere" / name).parent.mkdir(parents=True, exist_ok=True)
        (work / "elsewhere" / name).write_text(text, encoding="utf-8")
    for name, text in task.files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    if task.build:
        task.build(root)
    old = os.environ.get("HEARTH_HOME")
    os.environ["HEARTH_HOME"] = str(home)
    counted = Counting(model)
    t0 = time.monotonic()
    try:
        ctx = make_ctx(Config(folders=[str(root)]), lambda title, detail: True)   # the eval user approves every change...

        def choose(title: str, detail: str, options: list[str]) -> int:
            return len(options) - 1 if title.startswith("Allow Hearth to read") else 0   # ...but never opens other places

        ctx.choose = choose
        ctx.index.sync()
        if task.selected:
            ctx.selected = str(root / task.selected)
        agent = Agent(counted, ctx, max_steps)
        history: list[dict[str, str]] = []
        for prompt in [task.prompt, *task.then]:
            res = agent.run(prompt, history)
            remember(history, prompt, res)
        ok = task.check(root, res.answer)
        return {"id": task.id, "passed": ok, "steps": res.steps, "model_calls": counted.calls,
                "unusable_replies": counted.unusable, "seconds": round(time.monotonic() - t0, 1),
                "answer": res.answer[:200], "tags": task.tags}
    except ModelError as e:
        return {"id": task.id, "passed": False, "error": str(e), "tags": task.tags}
    finally:
        if old is None:
            os.environ.pop("HEARTH_HOME", None)
        else:
            os.environ["HEARTH_HOME"] = old
        shutil.rmtree(work, ignore_errors=True)


def selftest() -> int:
    bad = 0
    for t in TASKS:
        if not available(t):
            print(f"  [--] {t.id:22} skipped: needs the '{t.requires}' package")
            continue
        perfect = run_task(t, ScriptedModel(list(t.solution)))
        noop = run_task(t, ScriptedModel([A("I'm not sure how to help with that.")]))
        want_noop_pass = "safety" in t.tags   # only the "leave things alone" tasks may pass by doing nothing
        ok = bool(perfect["passed"]) and bool(noop["passed"]) == want_noop_pass
        print(f"  [{'ok' if ok else '!!'}] {t.id:22} perfect={perfect['passed']} do-nothing={noop['passed']} (expected {want_noop_pass})")
        bad += 0 if ok else 1
    print("selftest:", "all tasks are well-formed" if not bad else f"{bad} task(s) are broken")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="llama3.1:8b")
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--only", nargs="*", help="task ids")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=str(Path(__file__).parent / "results"))
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    tasks = [t for t in TASKS if (not a.only or t.id in a.only)]
    for t in [t for t in tasks if not available(t)]:
        print(f"  [--] {t.id:22} skipped: needs the '{t.requires}' package (pip install {t.requires})")
    tasks = [t for t in tasks if available(t)]
    model = OllamaModel(a.model, a.url)
    rows = []
    for t in tasks:
        r = run_task(t, model)
        rows.append(r)
        print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {t.id:22} steps={r.get('steps', '-')} bad_replies={r.get('unusable_replies', '-')} "
              f"{r.get('seconds', '-')}s {r.get('error', '')}")
    passed = sum(bool(r["passed"]) for r in rows)
    print(f"\n{a.model}: {passed}/{len(rows)} tasks passed")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    safe = a.model.replace(":", "_").replace("/", "_")
    (out / f"{safe}.json").write_text(json.dumps({"model": a.model, "passed": passed, "total": len(rows), "tasks": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
