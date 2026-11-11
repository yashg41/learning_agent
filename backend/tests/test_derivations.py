"""Derivation store and step verification.

The verification tests carry most of the weight here. The tutor runs on a small
model, so the thing that makes a derivation trustworthy is that sympy checked
it — and the failure that matters most is not "missed a wrong step" but "called
a correct step wrong", which would tell a learner a valid derivation is broken.
Both directions are asserted below.
"""

import tempfile

import pytest

from backend import derivations as D
from backend.mathverify import check_equal, verify_steps


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(D, "USERS_DIR", tempfile.mkdtemp())
    return "learner@example.com"


# --- Store ---


def test_blocks_append_rather_than_replace(store):
    """The builder writes in waves; wave two must not discard wave one."""
    doc = D.create_doc(store, "Gradient", "logistic_regression")
    D.append_blocks(store, doc["id"], [{"kind": "text", "content": "intuition"}])
    D.append_blocks(store, doc["id"], [{"kind": "latex", "content": "x^2"}])
    saved = D.append_blocks(store, doc["id"], [
        {"kind": "derivation", "steps": [{"expr": "a", "reason": "start"}]},
    ])
    assert [b["kind"] for b in saved["blocks"]] == ["text", "latex", "derivation"]


def test_update_block_keeps_id_and_siblings(store):
    doc = D.create_doc(store, "T")
    saved = D.append_blocks(store, doc["id"], [
        {"kind": "text", "content": "before"},
        {"kind": "derivation", "steps": [{"expr": "a", "reason": "r"}]},
    ])
    target = saved["blocks"][1]["id"]

    D.update_block(store, doc["id"], target,
                   steps=[{"expr": "a", "reason": "r", "verified": True}])

    after = D.get_doc(store, doc["id"])
    assert len(after["blocks"]) == 2                     # sibling survived
    assert after["blocks"][1]["id"] == target            # id not re-minted
    assert after["blocks"][1]["steps"][0]["verified"] is True


@pytest.mark.parametrize("bad", ["../../etc/passwd", "d_../x", "", "n_abc123"])
def test_traversal_ids_never_become_paths(store, bad):
    """Ids arrive from chat text, so they are untrusted input."""
    assert D.get_doc(store, bad) is None


def test_plot_src_cannot_escape_the_doc_dir():
    with pytest.raises(ValueError):
        D.validate_blocks([{"kind": "plot", "src": "../../../etc/passwd"}])


def test_unknown_block_kind_rejected():
    with pytest.raises(ValueError):
        D.validate_blocks([{"kind": "script", "content": "x"}])


def test_step_requires_an_expression():
    with pytest.raises(ValueError):
        D.validate_blocks([{"kind": "derivation", "steps": [{"reason": "no expr"}]}])


# --- Verification ---


@pytest.mark.parametrize("lhs,rhs", [
    (r"(a-b)^2", r"a^2-2ab+b^2"),
    (r"\frac{2x}{4}", r"\frac{x}{2}"),
    (r"\sqrt{ab}", r"\sqrt{a}\sqrt{b}"),
    # Holds only for positive reals. sympy will not assume that of a bare
    # Symbol, and without the positive-domain retry this common log step gets
    # reported as an error — a correct derivation called broken.
    (r"\log(x^n)", r"n\log(x)"),
])
def test_correct_steps_are_not_called_wrong(lhs, rhs):
    assert check_equal(lhs, rhs)["verified"] is True


@pytest.mark.parametrize("lhs,rhs", [
    (r"(a-b)^2", r"a^2+2ab+b^2"),      # sign flip
    (r"x^2+2x+1", r"x^2+1"),           # dropped term
    (r"2x", r"3x"),                    # off by a factor
])
def test_wrong_steps_are_caught(lhs, rhs):
    assert check_equal(lhs, rhs)["verified"] is False


@pytest.mark.parametrize("lhs,rhs", [
    (r"\text{by the chain rule}", r"x"),   # prose, not algebra
    (r"\foo{{{", r"x"),                    # unparseable
    (r"\log(x+y)", r"\log(x)+\log(y)"),    # genuinely ambiguous to the parser
])
def test_uncheckable_is_none_not_false(lhs, rhs):
    """None and False must stay distinct.

    "could not check" and "checked and it is wrong" mean very different things
    to a learner, and collapsing them would make the verified badge a lie.
    """
    assert check_equal(lhs, rhs)["verified"] is None


def test_verify_steps_marks_premise_then_checks_the_rest():
    steps = verify_steps([
        {"expr": r"(a-b)^2", "reason": "start"},
        {"expr": r"a^2-2ab+b^2", "reason": "expand"},
        {"expr": r"a^2+b^2", "reason": "dropped the cross term"},
    ])
    assert [s["verified"] for s in steps] == [None, True, False]
    assert steps[0]["check"] == "premise"


# --- Jobs ---


def test_job_reaches_ready_only_when_blocks_were_written(monkeypatch):
    """A builder that opens a doc but writes nothing must not report ready.

    Otherwise the chip sits at "ready" pointing at an empty pane — the same
    class of bug that produced 404s on Open for demos.
    """
    import asyncio

    from backend import mathjobs as MJ

    email = "learner@example.com"
    monkeypatch.setattr(D, "USERS_DIR", tempfile.mkdtemp())
    MJ.JOBS.clear()

    async def fake_builder(email, request, chat_session_id=None,
                           resume_builder_session=None, on_event=None,
                           build_ctx=None):
        build_ctx["doc_id"] = "d_abc123"      # opened...
        return "builder_session"              # ...but never wrote

    monkeypatch.setattr("backend.agent.run_derivation_builder", fake_builder)

    record = MJ.new_job(email, "chat1", "Test derivation")
    asyncio.run(MJ._run_build(record))

    assert MJ.JOBS[record["job_id"]]["state"] == MJ.FAILED


def test_job_ownership_is_enforced(monkeypatch):
    """JOBS is keyed by job_id alone, so without the check one learner's id
    would read another learner's record — including their request text."""
    from backend import mathjobs as MJ

    monkeypatch.setattr(D, "USERS_DIR", tempfile.mkdtemp())
    MJ.JOBS.clear()
    record = MJ.new_job("owner@example.com", None, "Mine", request="secret")

    assert MJ.get_job("owner@example.com", record["job_id"]) is not None
    assert MJ.get_job("someone-else@example.com", record["job_id"]) is None


def test_sweep_marks_orphaned_jobs_failed(monkeypatch):
    """A restart leaves a BUILDING record on disk with nothing in JOBS."""
    from backend import mathjobs as MJ

    monkeypatch.setattr(D, "USERS_DIR", tempfile.mkdtemp())
    MJ.JOBS.clear()
    record = MJ.new_job("learner@example.com", None, "Orphan")
    MJ.update_job(record["job_id"], state=MJ.BUILDING)
    MJ.JOBS.clear()                                  # simulate the restart

    MJ.sweep_stale("learner@example.com")

    revived = MJ.get_job("learner@example.com", record["job_id"])
    assert revived["state"] == MJ.FAILED
    assert "restart" in revived["error"]


# --- Delimiter contract ---
#
# The frontend normalizes single-$ math to $$ and strips delimiters the pane
# supplies itself. These assert the CONTRACT the builder is told to follow, so
# a prompt edit that drops the rule shows up as a test failure rather than as
# raw LaTeX on the learner's screen.


def test_latex_and_expr_are_stored_bare(store):
    """`latex` content and step `expr` carry no delimiters.

    The pane wraps them in $$ itself; storing them wrapped renders a literal
    $$ on screen. The store does not enforce this — it is what the builder
    prompt and skill instruct — so this documents the shape the renderer
    assumes.
    """
    doc = D.create_doc(store, "T")
    saved = D.append_blocks(store, doc["id"], [
        {"kind": "latex", "content": r"\sigma(z) = \frac{1}{1+e^{-z}}"},
        {"kind": "derivation", "steps": [
            {"expr": r"\sigma'(z)", "reason": "differentiate"},
        ]},
    ])
    assert not saved["blocks"][0]["content"].startswith("$")
    assert not saved["blocks"][1]["steps"][0]["expr"].startswith("$")


def test_text_blocks_keep_their_markup_verbatim(store):
    """Prose is stored exactly as written — markdown and $$ included.

    Rendering is the frontend's job. If the store normalized here instead,
    a fix would need a data migration rather than a page reload.
    """
    content = r"The **key** term is $$\sigma'(z)$$ and it cancels."
    doc = D.create_doc(store, "T")
    saved = D.append_blocks(store, doc["id"], [
        {"kind": "text", "content": content},
    ])
    assert saved["blocks"][0]["content"] == content
