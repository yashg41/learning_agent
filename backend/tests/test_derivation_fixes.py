"""What a live derivation build showed, pinned.

A real builder, asked for d/dx x^2, produced a doc where every step read
"unchecked", the same derivation appeared four times, three blocks shared one
id, and a `pip install` inside run_code landed in the server's own .venv.

  - Equation steps: the parser turns "a + b = b + a" into True, not an
    expression, and verify_steps reported every such step "ambiguous notation —
    add braces". The builder chased braces through four rewrites of a correct
    derivation.
  - Appending was the only write, so each rewrite went BELOW the wrong copy,
    and a builder-supplied "id" was kept verbatim.
  - Cells inherited the server's PATH, whose first entry was the server's venv.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from backend import derivations, mathjobs
from backend.mathverify import split_sides, verify_steps


def _v(*exprs):
    return [s["verified"] for s in verify_steps([{"expr": e} for e in exprs])]


# --- Equation steps --------------------------------------------------------


def test_the_live_runs_equation_steps_now_verify():
    """The exact block the builder rewrote four times. Correct all along."""
    assert _v(
        r"\frac{d}{dx}x^2 = \lim_{h \to 0} \frac{(x+h)^2 - x^2}{h}",
        r"\lim_{h \to 0} \frac{x^2 + 2xh + h^2 - x^2}{h}",
        r"\lim_{h \to 0} \frac{2xh + h^2}{h} = \lim_{h \to 0} (2x + h)",
        r"\frac{d}{dx}x^2 = 2x",
    ) == [None, True, True, True]


def test_a_wrong_equation_is_still_caught():
    assert _v("x", r"(a-b)^2 = a^2 + b^2") == [None, False]


def test_every_side_of_a_chain_is_checked():
    assert _v("x", r"(a-b)^2 = a^2-2ab+b^2 = a^2+b^2") == [None, False]
    assert _v("x", r"(a-b)^2 = a^2-2ab+b^2 = b^2-2ab+a^2") == [None, True]


def test_an_expression_step_follows_the_last_side_above():
    """"f = (a-b)^2" leaves the derivation at (a-b)^2, not at f."""
    assert _v(r"f = (a-b)^2", r"a^2-2ab+b^2") == [None, True]


@pytest.mark.parametrize("definition", [
    r"y = mx + b",
    r"L(w, \lambda) = w - \lambda",
    r"\hat{y} = 3x",
    r"\Sigma_{i} = a + b",
])
def test_a_definition_is_never_called_wrong(definition):
    """Numerically y != mx + b, but the step DEFINES y. Calling it false would
    tell the learner a correct definition is broken."""
    assert _v("x", definition) == [None, None]


def test_a_formula_on_the_left_is_not_mistaken_for_a_name():
    """\\frac{a}{b} is a claim, not a name being defined."""
    assert _v("x", r"\frac{a}{b} = \frac{a}{2b}") == [None, False]


def test_inequalities_are_unchecked_not_wrong():
    steps = verify_steps([{"expr": "x"}, {"expr": r"x^2 \ge 0"}, {"expr": "x < y"}])
    assert [s["verified"] for s in steps] == [None, None, None]
    assert "inequality" in steps[1]["check"]


def test_equals_inside_a_group_is_not_a_split():
    assert split_sides(r"\sum_{i=1}^{n} x_i") == ([r"\sum_{i=1}^{n} x_i"], "")
    assert split_sides(r"\left(x\right) = 2") == ([r"\left(x\right)", "2"], "")


def test_non_expressions_are_not_blamed_on_braces():
    """The misleading message is what sent the builder chasing braces."""
    from backend.mathverify import check_equal

    assert "braces" not in check_equal("a + b = b + a", "x")["detail"]


# --- Ids and stamps belong to the server -----------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(derivations, "USERS_DIR", str(tmp_path / "users"))
    return "learner@example.com"


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    import backend.sandbox as sandbox

    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setattr(sandbox, "SCRATCH_ROOT", root)
    return root


def _tool(store, name, args, build_ctx):
    """Through the real MCP request path."""
    import mcp.types as types

    from backend.tools import create_learning_tools

    udir = os.path.join(derivations.USERS_DIR, store)
    os.makedirs(udir, exist_ok=True)
    server = create_learning_tools(udir, None, None, build_ctx, "pymentor")
    caller = server["instance"].request_handlers[types.CallToolRequest]

    async def go():
        return await caller(types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments=args),
        ))

    return asyncio.run(go()).root.content[0].text


def _wave(scratch, name, blocks):
    path = scratch / name
    path.write_text(json.dumps(blocks))
    return str(path)


def test_a_builder_cannot_choose_ids_or_stamp_steps(store, scratch):
    doc = derivations.create_doc(store, "T")
    first = derivations.append_blocks(store, doc["id"], [
        {"kind": "text", "content": "a"},
    ])["blocks"][0]["id"]

    out = _tool(store, "derivation_write", {
        "doc_id": doc["id"],
        "blocks_path": _wave(scratch, "w.json", [
            {"id": first, "kind": "derivation", "title": "t", "steps": [
                {"expr": "x", "reason": "r", "verified": True, "check": "trust me"},
                {"expr": "2x", "reason": "r", "verified": True},
            ]},
        ]),
    }, {})

    assert '"status": "written"' in out, out
    blocks = derivations.get_doc(store, doc["id"])["blocks"]
    assert len({b["id"] for b in blocks}) == 2, "a builder-supplied id was kept"
    assert [s["verified"] for s in blocks[1]["steps"]] == [None, None]
    assert all(s["check"] == "" for s in blocks[1]["steps"])


def _seed(store):
    """intro text, a verified derivation, a closing text."""
    doc = derivations.create_doc(store, "T")
    doc = derivations.append_blocks(store, doc["id"], [
        {"kind": "text", "content": "intro"},
        {"kind": "derivation", "title": "d", "steps": [
            {"expr": "(a-b)^2", "reason": "start"},
            {"expr": "a^2-2ab+b^2", "reason": "expand"},
        ]},
        {"kind": "text", "content": "outro"},
    ])
    block = doc["blocks"][1]
    derivations.update_block(store, doc["id"], block["id"],
                             steps=verify_steps(block["steps"]))
    return derivations.get_doc(store, doc["id"])


def test_checkout_edit_update_replaces_the_block_where_it_stands(store, scratch):
    doc = _seed(store)
    ids = [b["id"] for b in doc["blocks"]]
    ctx = {"scratch_dir": str(scratch)}

    path = json.loads(_tool(store, "get_derivation_json",
                            {"doc_id": doc["id"]}, ctx))["path"]
    assert path.startswith(str(scratch))

    # What Edit would do: change one step of the middle block.
    text = Path(path).read_text().replace("a^2-2ab+b^2", "a^2+b^2")
    Path(path).write_text(text)

    out = json.loads(_tool(store, "derivation_update",
                           {"doc_id": doc["id"], "blocks_path": path}, ctx))

    after = derivations.get_doc(store, doc["id"])["blocks"]
    assert [b["id"] for b in after] == ids, "position or id changed"
    assert after[1]["steps"][1]["expr"] == "a^2+b^2"
    # The edited block's old tick must not survive onto the new step.
    assert after[1]["steps"][1]["verified"] is None
    assert out["next"] == f"Call derivation_verify on: {ids[1]}"


def test_an_unchanged_block_keeps_its_stored_stamps(store, scratch):
    doc = _seed(store)
    ctx = {"scratch_dir": str(scratch)}
    path = json.loads(_tool(store, "get_derivation_json",
                            {"doc_id": doc["id"]}, ctx))["path"]
    Path(path).write_text(Path(path).read_text().replace("outro", "closing"))

    out = json.loads(_tool(store, "derivation_update",
                           {"doc_id": doc["id"], "blocks_path": path}, ctx))

    after = derivations.get_doc(store, doc["id"])["blocks"]
    assert after[1]["steps"][1]["verified"] is True
    assert after[2]["content"] == "closing"
    assert "next" not in out


def test_stamps_in_the_file_are_not_trusted(store, scratch):
    doc = _seed(store)
    ctx = {"scratch_dir": str(scratch)}
    path = json.loads(_tool(store, "get_derivation_json",
                            {"doc_id": doc["id"]}, ctx))["path"]
    blocks = json.loads(Path(path).read_text())
    blocks[1]["steps"][1]["expr"] = "a^2+b^2"
    blocks[1]["steps"][1]["verified"] = True          # a builder "fixing" it
    Path(path).write_text(json.dumps(blocks))

    _tool(store, "derivation_update", {"doc_id": doc["id"], "blocks_path": path}, ctx)

    assert derivations.get_doc(store, doc["id"])["blocks"][1]["steps"][1]["verified"] is None


def test_removing_a_block_from_the_file_deletes_it(store, scratch):
    doc = _seed(store)
    ctx = {"scratch_dir": str(scratch)}
    path = json.loads(_tool(store, "get_derivation_json",
                            {"doc_id": doc["id"]}, ctx))["path"]
    blocks = json.loads(Path(path).read_text())
    Path(path).write_text(json.dumps([blocks[0], blocks[2]]))

    _tool(store, "derivation_update", {"doc_id": doc["id"], "blocks_path": path}, ctx)

    after = derivations.get_doc(store, doc["id"])["blocks"]
    assert [b["content"] for b in after] == ["intro", "outro"]


def test_duplicate_and_unknown_ids_in_the_file_are_reminted(store, scratch):
    doc = _seed(store)
    first = doc["blocks"][0]["id"]
    path = _wave(scratch, "edit.json", [
        {"id": first, "kind": "text", "content": "one"},
        {"id": first, "kind": "text", "content": "two"},
        {"id": "b_madeup1", "kind": "text", "content": "three"},
    ])

    _tool(store, "derivation_update",
          {"doc_id": doc["id"], "blocks_path": path}, {"scratch_dir": str(scratch)})

    ids = [b["id"] for b in derivations.get_doc(store, doc["id"])["blocks"]]
    assert ids[0] == first
    assert len(set(ids)) == 3
    assert "b_madeup1" not in ids


def test_a_plot_cannot_be_conjured_from_the_file(store, scratch):
    doc = _seed(store)
    path = _wave(scratch, "edit.json", [
        {"kind": "plot", "src": "_plot_999.svg", "caption": "c"},
    ])

    out = _tool(store, "derivation_update",
                {"doc_id": doc["id"], "blocks_path": path},
                {"scratch_dir": str(scratch)})

    assert out.startswith("Derivation rejected:")
    assert len(derivations.get_doc(store, doc["id"])["blocks"]) == 3


def test_the_tutor_cannot_check_out_or_replace(store, scratch):
    doc = _seed(store)
    for name, args in (("get_derivation_json", {"doc_id": doc["id"]}),
                       ("derivation_update", {"doc_id": doc["id"], "blocks_path": "x"})):
        assert "not available" in _tool(store, name, args, None)


def test_a_revision_made_through_update_reaches_ready(store, scratch, monkeypatch):
    import backend.agent as agent

    doc = _seed(store)
    monkeypatch.setattr(mathjobs, "JOBS", {})
    monkeypatch.setattr(mathjobs, "TASKS", {})
    monkeypatch.setattr(mathjobs, "CANCELS", {})

    async def build(email, request, chat_session_id=None,
                    resume_builder_session=None, on_event=None, build_ctx=None):
        path = derivations.copy_doc_to_scratch(email, doc["id"], str(scratch))
        Path(path).write_text(Path(path).read_text().replace("outro", "closing"))
        derivations.replace_blocks(email, doc["id"],
                                   json.loads(Path(path).read_text()))
        return "b1"

    monkeypatch.setattr(agent, "run_derivation_builder", build)
    record = mathjobs.new_job(store, "chat-1", "T", request="reword the ending",
                              base_doc_id=doc["id"])

    asyncio.run(mathjobs._run_build(record))

    assert mathjobs.JOBS[record["job_id"]]["state"] == mathjobs.READY


# --- A cell's pip is the learner's pip -------------------------------------


def test_a_cells_path_puts_the_learners_venv_first_and_drops_the_servers(monkeypatch):
    from backend.sandbox import _cell_path

    server_venv = "/srv/app/.venv"
    monkeypatch.setattr(sys, "prefix", server_venv)
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setenv("VIRTUAL_ENV", "/other/checkout/.venv")
    monkeypatch.setenv("PATH", os.pathsep.join([
        "/other/checkout/.venv/bin", f"{server_venv}/bin", "/usr/local/bin", "/usr/bin",
    ]))

    parts = _cell_path(Path("/data/u/venv")).split(os.pathsep)

    assert parts[0] == "/data/u/venv/bin"
    assert f"{server_venv}/bin" not in parts
    assert "/other/checkout/.venv/bin" not in parts
    assert parts[1:] == ["/usr/local/bin", "/usr/bin"]


def test_a_base_interpreter_keeps_its_bin(monkeypatch):
    """Not a venv: its bin/ may be where system tools live, so keep it."""
    from backend.sandbox import _cell_path

    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")

    assert _cell_path(Path("/data/u/venv")).split(os.pathsep) == [
        "/data/u/venv/bin", "/usr/bin",
    ]


# --- e is Euler's number when that is the reading that holds -----------------


def test_the_sigmoid_step_live_marked_wrong_now_verifies():
    """Correct maths, reported false: the parser read e as a variable, so the
    derivative picked up a log(e) factor. Seen on a live sigmoid build."""
    assert _v("x", r"\frac{d}{dz}(1+e^{-z})^{-1} = \frac{e^{-z}}{(1+e^{-z})^2}") == [None, True]


def test_e_as_euler_does_not_rescue_a_wrong_step():
    assert _v("x", r"\frac{d}{dz}(1+e^{-z})^{-1} = \frac{e^{z}}{(1+e^{-z})^2}") == [None, False]


def test_e_as_a_plain_variable_still_works():
    assert _v("x", r"(e+1)^2 = e^2 + 2e + 1") == [None, True]


# --- Derivatives -------------------------------------------------------------
#
# Live SVM derivation: every "differentiate the Lagrangian" step showed ? —
# "could not parse: VisitError". sympy's lark parser has no derivative rule at
# all, so no notation the builder chose could ever be checked, and these are
# the steps a derivation most needs checked.

def test_the_live_svm_derivative_steps_now_verify():
    assert _v("x", r"\frac{d}{d w_1}\left(\frac{1}{2}\cdot w_1^2 - 2\cdot a_1\cdot w_1"
                   r" - 3\cdot a_3\cdot w_1\right) = w_1 - 2\cdot a_1 - 3\cdot a_3") == [None, True]
    assert _v("x", r"\frac{d}{d b}\left(-\left(a_1-a_2+a_3\right)\cdot b\right)"
                   r" = -\left(a_1-a_2+a_3\right)") == [None, True]


def test_a_wrong_derivative_is_caught():
    assert _v("x", r"\frac{d}{dx}\left(x^3\right) = 2x^2") == [None, False]


@pytest.mark.parametrize("expr", [
    r"\frac{\partial}{\partial w_{1}}{\left(w_1^2 b\right)} = 2 w_1 b",   # \partial, braced group
    r"\frac{d}{dx}\left[x^3\right] = 3x^2",                               # square brackets
    r"\frac{d^2}{dx^2}\left(x^3\right) = 6x",                             # second derivative
    r"\frac{d}{dx}\left(\frac{d}{dx}\left(x^4\right)\right) = 12x^2",     # nested
    r"3 + \frac{d}{dx}\left(x^2\right) = 2x + 3",                         # inside a sum
    r"\frac{d}{dx}\left(x^2\right)^{3} = 6x^5",                           # power on the operand
])
def test_derivative_notations_verify(expr):
    assert _v("x", expr) == [None, True]


def test_the_derivative_of_a_name_stays_unknown():
    """dL/dw names a function; there is no expression to differentiate."""
    assert _v("x", r"\frac{\partial L}{\partial w} = w - a") == [None, None]


# --- Juxtaposition after a power ----------------------------------------------
#
# The same build wrote "2\cdot a_1\cdot w_1" everywhere. Not a whim: the parser
# rejects "x^2 b" (a power followed by a factor), so \cdot was the price of a
# tick. The checker now makes that product explicit itself.

def test_a_power_followed_by_a_factor_parses():
    assert _v("x", r"w_1^2 b + b = b w_1^2 + b") == [None, True]
    assert _v("x", r"x^2 y - y x^2 = 0") == [None, True]


# --- Bare maths in prose -------------------------------------------------------
#
# The same build's reasons read "(1/2)||w||^2 is w" and "x_11 = 2": the pane
# typesets only $$…$$, so all of it reached the learner as raw text.

def test_bare_maths_in_reasons_cells_and_captions_is_reported():
    blocks = [
        {"id": "b1", "kind": "derivation", "steps": [{"expr": "w", "reason": "the derivative of (1/2)||w||^2 is w"}]},
        {"id": "b2", "kind": "table", "headers": ["x_i"], "rows": [["a_1"]], "caption": "with x_11 = 2"},
    ]
    found = " ".join(derivations.bare_math_warnings(blocks))
    for where in ("b1 step 1 reason", "b2 header", "b2 cell", "b2 caption"):
        assert where in found


def test_wrapped_maths_and_code_are_not_reported():
    blocks = [
        {"id": "b1", "kind": "derivation", "steps": [{"expr": "w", "reason": r"power rule on $$w_1^2$$ and $$\|w\|^2$$"}]},
        {"id": "b2", "kind": "table", "headers": ["point"], "rows": [["$$a_1$$", "1/4"]], "caption": "a = (1/4, 1/4, 0)."},
        {"id": "b3", "kind": "text", "content": "Call it `x_1` in code. A plain sentence."},
    ]
    assert derivations.bare_math_warnings(blocks) == []
