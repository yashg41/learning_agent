"""The UI hears about compaction when it starts, not when it ends.

Compaction summarises a session that has outgrown the context window. It takes
minutes and emits no tokens, so the thinking indicator ticks upward and reads
as a hang. The first fix relabelled the indicator on `compact_boundary` — and
the label still showed only for a moment, right before the first token removed
the indicator. The boundary is the END marker. The CLI's own sequence (2.1.38):

    setSDKStatus("compacting")   -> system/status, status="compacting"
    ...summarise (the minutes)...
    setSDKStatus(null)           -> system/status, status=null
    compact_boundary

So the signal that matters is the `status` message, and the boundary is a
fallback for a CLI that does not send it.
"""

from claude_agent_sdk import SystemMessage

from backend.agent import compaction_event


def status(value, **extra):
    return SystemMessage(
        subtype="status",
        data={"type": "system", "subtype": "status", "status": value, **extra},
    )


def test_status_compacting_is_the_start():
    event = compaction_event(status("compacting"))
    assert event is not None
    assert event["type"] == "compacting"
    assert event["phase"] == "start"


def test_status_null_is_not_an_event():
    # Sent when compaction finishes; the boundary that follows covers it.
    assert compaction_event(status(None)) is None


def test_permission_mode_status_is_not_compaction():
    # Same subtype, carries a permission-mode change and status null.
    assert compaction_event(status(None, permissionMode="acceptEdits")) is None


def test_boundary_still_reports_and_carries_metadata():
    msg = SystemMessage(
        subtype="compact_boundary",
        data={"compactMetadata": {"trigger": "auto", "preTokens": 187000}},
    )
    event = compaction_event(msg)
    assert event == {"type": "compacting", "phase": "end",
                     "trigger": "auto", "pre_tokens": 187000}


def test_start_comes_before_end_in_the_cli_sequence():
    sequence = [
        status("compacting"),
        status(None),
        SystemMessage(subtype="compact_boundary", data={}),
    ]
    phases = [e["phase"] for m in sequence if (e := compaction_event(m))]
    assert phases == ["start", "end"]


def test_other_messages_are_ignored():
    assert compaction_event(SystemMessage(subtype="init", data={})) is None
    assert compaction_event(object()) is None
