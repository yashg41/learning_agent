"""Demo storage, build jobs, and handler validation.

Covers the regressions behind the "demos never save" bug and the move to
background builds. Nothing here calls the model — the agent runs are covered
by hand, since they cost API calls and take ~30s each.
"""

import shutil

import pytest

from backend import demojobs
from backend.demos import (
    _missing_handlers,
    delete_demo,
    demos_dir,
    get_demo,
    list_demos,
    save_demo,
    update_demo,
    validate_demo_html,
)

HTML_V1 = (
    "<html><body><h1 id='out'>0</h1>"
    "<button onclick='go()'>+</button>"
    "<script>function go(){}</script></body></html>"
)
HTML_V2 = HTML_V1.replace(">0<", ">1<")


@pytest.fixture
def email(tmp_path_factory):
    who = f"__test_{tmp_path_factory.mktemp('d').name}__"
    yield who
    shutil.rmtree(demos_dir(who), ignore_errors=True)


# --- Storage ---------------------------------------------------------------


def test_save_records_session_links(email):
    """Both session ids are what make a demo refinable and listable later."""
    meta = save_demo(
        email, "T", HTML_V1,
        builder_session_id="b-1", chat_session_id="c-1",
    )
    assert meta["version"] == 1
    assert meta["builder_session_id"] == "b-1"
    assert meta["chat_session_id"] == "c-1"


def test_update_keeps_id_and_bumps_version(email):
    """A refinement must edit in place.

    save_demo always mints a new id, so without update_demo every edit would
    leave a duplicate behind and churn the MAX_DEMOS_PER_USER eviction.
    """
    meta = save_demo(email, "T", HTML_V1, builder_session_id="b-1")
    updated = update_demo(email, meta["id"], HTML_V2)

    assert updated["id"] == meta["id"]
    assert updated["version"] == 2
    assert updated["builder_session_id"] == "b-1"  # survives the edit
    assert ">1<" in get_demo(email, meta["id"])["html"]
    assert len([d for d in list_demos(email) if d["id"] == meta["id"]]) == 1


def test_update_missing_demo_returns_none(email):
    assert update_demo(email, "d_nope", HTML_V1) is None


def test_update_upgrades_legacy_metadata(email):
    """Demos saved before versioning existed must still be editable."""
    import json

    from backend.demos import _meta_path

    meta = save_demo(email, "T", HTML_V1)
    stale = json.load(open(_meta_path(email, meta["id"])))
    for key in ("version", "updated_at", "builder_session_id"):
        stale.pop(key, None)
    with open(_meta_path(email, meta["id"]), "w") as f:
        json.dump(stale, f)

    assert update_demo(email, meta["id"], HTML_V2)["version"] == 2


# --- Handler validation ----------------------------------------------------
#
# Each of these rejected a working demo before. The validator only has to catch
# a plain typo; anything it cannot resolve statically must pass.


@pytest.mark.parametrize("script", [
    "const reset = x => { };",              # single-param arrow, no parens
    "const reset = () => { };",             # zero-arg arrow
    "const reset = function(){};",          # function expression
    "function reset(){}",                   # declaration
    "var o = {reset: function(){}};",       # object literal
])
def test_defined_handlers_accepted(script):
    html = f"<button onclick='reset()'>x</button><script>{script}</script>"
    assert _missing_handlers(html) == set()


@pytest.mark.parametrize("handler", [
    "requestAnimationFrame(step)",  # recommended in references/patterns.md
    "setTimeout(step, 100)",
    "console.log(1)",
    "app.reset()",                  # method call — not a top-level name
])
def test_runtime_names_not_flagged(handler):
    html = f"<button onclick='{handler}'>x</button><script>function step(){{}}</script>"
    assert _missing_handlers(html) == set()


def test_real_typo_still_caught():
    html = "<button onclick='gooo()'>x</button><script>function go(){}</script>"
    assert _missing_handlers(html) == {"gooo"}


def test_external_resources_rejected():
    with pytest.raises(ValueError):
        validate_demo_html(
            "<html><body><script src='https://cdn.example/x.js'></script></body></html>"
        )


def test_fragment_rejected():
    with pytest.raises(ValueError):
        validate_demo_html("<p>not a document</p>")


# --- Build jobs ------------------------------------------------------------


def test_job_survives_restart_as_failed(email):
    """An orphaned job must resolve, or the UI chip spins forever."""
    record = demojobs.new_job(email, "chat-1", "T", request="build it")
    demojobs.update_job(record["job_id"], state=demojobs.BUILDING)

    # Simulate a restart: in-memory state is gone, disk records remain.
    demojobs.JOBS.clear()
    demojobs.CANCELS.clear()
    demojobs.sweep_stale(email)

    after = demojobs.get_job(email, record["job_id"])
    assert after["state"] == demojobs.FAILED
    assert "restart" in after["error"]


def test_jobs_persist_for_reconnect(email):
    """The frontend rebuilds chip state from this after a refresh."""
    record = demojobs.new_job(email, "chat-1", "T")
    demojobs.JOBS.clear()

    listed = demojobs.list_jobs(email)
    assert [j["job_id"] for j in listed] == [record["job_id"]]


# --- Referencing a demo from the chat --------------------------------------


def test_digest_is_a_fraction_of_the_html(email):
    """The digest exists so the html never enters the transcript.

    Anything sent lands in the SDK session and is re-sent on every later turn,
    so a 17KB demo would cost ~4200 tokens per turn for the rest of the
    conversation.
    """
    from backend.demos import demo_digest

    # A realistic demo, not the 110-char fixture: the digest has a fixed
    # header, so it only pays off at the size demos actually are (~17KB).
    padding = "\n".join(
        f"  <div class='row-{n}'>step {n}</div>" for n in range(400)
    )
    big = HTML_V1.replace("</body>", f"{padding}</body>")
    meta = save_demo(email, "T", big, summary="Click plus to count.")
    digest = demo_digest(email, meta["id"])

    assert digest is not None
    # The point of the whole design: an order of magnitude smaller.
    assert len(digest) < len(big) / 10
    assert "T" in digest and meta["id"] in digest
    assert "Click plus to count." in digest      # summary rides along
    assert "out" in digest                        # element id
    assert "go" in digest                         # function name
    assert "<script>" not in digest               # never raw markup


def test_digest_reports_a_broken_lookup(email):
    """The common "why doesn't this button work" answer, without the source."""
    from backend.demos import demo_digest

    broken = (
        "<html><body><h1 id='count'>0</h1>"
        "<button onclick='go()'>x</button>"
        "<script>function go(){ document.getElementById('counter').textContent = 1; }"
        "</script></body></html>"
    )
    meta = save_demo(email, "T", broken)
    digest = demo_digest(email, meta["id"])
    assert "WARNING" in digest
    assert "counter" in digest


def test_digest_includes_the_selection(email):
    from backend.demos import demo_digest

    meta = save_demo(email, "T", HTML_V1)
    digest = demo_digest(
        email, meta["id"],
        {"text": "Reset", "tag": "button", "ids": ["reset-btn"]},
    )
    assert 'They highlighted: "Reset"' in digest
    assert "reset-btn" in digest


def test_digest_missing_demo(email):
    from backend.demos import demo_digest

    assert demo_digest(email, "d_nope") is None


def test_session_demos_falls_back_when_none_match(email):
    """A strict session match hides demos the learner is looking at.

    Demos built before chat_session_id existed carry no such field, and an
    edit-branch fork or restore mints a session id no existing demo has.
    """
    from backend.demos import format_session_demos

    save_demo(email, "Orphan", HTML_V1)  # no chat_session_id
    listed = format_session_demos(email, "some-unrelated-session")
    assert "Orphan" in listed
    assert "none built in this session" in listed


def test_session_demos_prefers_this_session(email):
    from backend.demos import format_session_demos

    save_demo(email, "Older", HTML_V1)
    save_demo(email, "Mine", HTML_V1, chat_session_id="sess-1")

    listed = format_session_demos(email, "sess-1")
    assert "Mine" in listed
    assert "Older" not in listed
    assert "none built in this session" not in listed


# --- Cross-user isolation --------------------------------------------------
#
# There is no auth: the email in the request path is the whole identity model.
# That makes these checks the only thing keeping one learner's data away from
# another, so they are worth pinning down.


def test_job_is_not_readable_by_another_user(email):
    """JOBS is keyed by job_id alone, so ownership must be checked explicitly.

    Regression: the in-memory branch of get_job ignored email entirely, so a
    job id read back another learner's record — including the prompt they
    typed.
    """
    mine = demojobs.new_job(email, "s1", "Mine", request="my private prompt")

    assert demojobs.get_job("someone-else@example.com", mine["job_id"]) is None
    assert demojobs.get_job(email, mine["job_id"])["request"] == "my private prompt"


def test_job_cannot_be_cancelled_by_another_user(email):
    """request_cancel routes through get_job, so it inherited the same hole."""
    mine = demojobs.new_job(email, "s1", "Mine")

    assert demojobs.request_cancel("someone-else@example.com", mine["job_id"]) is False
    assert demojobs.get_job(email, mine["job_id"])["state"] == demojobs.QUEUED
    # The owner can still cancel it.
    assert demojobs.request_cancel(email, mine["job_id"]) is True


def test_list_jobs_is_scoped_to_one_user(email):
    mine = demojobs.new_job(email, "s1", "Mine")
    theirs = demojobs.new_job("other@example.com", "s2", "Theirs")

    ids = [j["job_id"] for j in demojobs.list_jobs(email)]
    assert mine["job_id"] in ids
    assert theirs["job_id"] not in ids

    shutil.rmtree(demos_dir("other@example.com"), ignore_errors=True)


def test_demos_with_the_same_title_stay_separate(email):
    other = "other@example.com"
    try:
        a = save_demo(email, "Counter", HTML_V1)
        b = save_demo(other, "Counter", HTML_V2)

        assert a["id"] != b["id"]
        assert get_demo(other, a["id"]) is None      # not visible across users
        assert ">1<" in get_demo(other, b["id"])["html"]
        assert [d["id"] for d in list_demos(email)] == [a["id"]]
    finally:
        shutil.rmtree(demos_dir(other), ignore_errors=True)


@pytest.mark.parametrize("bad_id", [
    "../../../etc/passwd",
    "..",
    "d_../x",
    "",
    "_index",          # would clobber the picker's index file
])
def test_malformed_demo_id_is_refused(email, bad_id):
    """Ids are interpolated into a path and the MCP tools pass model-supplied
    ones, so a `..` segment must not escape the learner's demos directory."""
    from backend.demos import get_demo_meta

    assert get_demo(email, bad_id) is None
    assert get_demo_meta(email, bad_id) is None
    assert update_demo(email, bad_id, HTML_V1) is None
    assert delete_demo(email, bad_id) is False


def test_cancel_is_terminal(email):
    record = demojobs.new_job(email, "chat-1", "T")
    assert demojobs.request_cancel(email, record["job_id"]) is True
    assert demojobs.get_job(email, record["job_id"])["state"] == demojobs.CANCELLED
    # Already finished — nothing left to cancel.
    assert demojobs.request_cancel(email, record["job_id"]) is False
