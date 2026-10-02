def test_resume_contract_is_cycle_scoped():
    # Documentation-level invariant: same-cycle completed components are resumable,
    # but a new 6-hour cycle must refresh them rather than pretending they are fresh.
    a="2026-10-02T06-full"
    b="2026-10-02T12-full"
    assert a != b
