from concertdb import state


def test_interval_doubles_then_resets(conn):
    state.ensure(conn, "x", "k")
    intervals = []
    for changed in (False, False, False, True, False):
        state.mark_checked(conn, "x", "k", changed=changed, min_days=1, max_days=6)
        intervals.append(state.get(conn, "x", "k")["interval_days"])
    assert intervals == [2, 4, 6, 1, 2]


def test_due_order_and_failures(conn):
    state.ensure(conn, "x", "low", priority=1)
    state.ensure(conn, "x", "high", priority=9)
    assert [r["key"] for r in state.due(conn, "x")] == ["high", "low"]
    state.mark_failed(conn, "x", "high")
    assert [r["key"] for r in state.due(conn, "x")] == ["low"]
    assert state.get(conn, "x", "high")["failures"] == 1


def test_ensure_keeps_progress(conn):
    state.ensure(conn, "x", "k", priority=1)
    state.save_cursor(conn, "x", "k", {"next_page": 4})
    state.ensure(conn, "x", "k", priority=3)
    row = state.get(conn, "x", "k")
    assert row["priority"] == 3 and state.get_cursor(row) == {"next_page": 4}


def test_usage(conn):
    state.record_usage(conn, "setlistfm", 2)
    state.record_usage(conn, "llm", 1, usd=0.25)
    assert state.requests_today(conn, "setlistfm") == 2
    assert state.usd_this_month(conn) == 0.25
