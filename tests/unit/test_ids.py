from linguawiki.ids import EventId, IdPrefix, WorkspaceId, new_id, validate_id


def test_ids_are_prefixed_and_timestamp_sortable() -> None:
    earlier = new_id(IdPrefix.EVENT, timestamp_ms=1)
    later = new_id(IdPrefix.EVENT, timestamp_ms=2)

    assert earlier.startswith("evt_")
    assert earlier < later
    assert validate_id(earlier, IdPrefix.EVENT) == earlier


def test_domain_types_reject_wrong_prefix() -> None:
    event_id = EventId.new()

    try:
        WorkspaceId(event_id)
    except ValueError as error:
        assert "expected wsp_" in str(error)
    else:
        raise AssertionError("wrong prefix was accepted")
