from horus.zones import all_zones, in_kind, zone_by_id, zone_for, zones_containing


def test_registry_is_wellformed() -> None:
    zones = all_zones()
    assert zones, "registry must not be empty"
    ids = [z.zone_id for z in zones]
    assert len(ids) == len(set(ids)), "zone ids must be unique"
    for z in zones:
        assert len(z.polygon) >= 3
        for lat, lon in z.polygon:
            assert -90 <= lat <= 90 and -180 <= lon <= 180


def test_point_over_singapore_strait_attributes_to_sensitive_corridor() -> None:
    # Over the strait: inside the corridor watch box (sensitive zones win attribution).
    zone = zone_for(1.15, 103.85)
    assert zone is not None and zone.zone_id == "sg-strait-corridor"
    assert zone.sensitive


def test_changi_terminal_membership() -> None:
    assert in_kind(1.36, 103.99, "terminal")
    containing = {z.zone_id for z in zones_containing(1.36, 103.99)}
    assert "changi-terminal" in containing


def test_open_ocean_is_no_zone() -> None:
    assert zone_for(-30.0, 90.0) is None


def test_zone_by_id_roundtrip() -> None:
    for z in all_zones():
        assert zone_by_id(z.zone_id) is z
    assert zone_by_id("nope") is None
