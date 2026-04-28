"""Round-trip NED ↔ WGS84 sanity checks using Mackenzie SP as origin."""
from constructive_airsim_ms.sim.coordinates import ned_to_wgs84, wgs84_to_ned

ORIGIN = (-23.5467, -46.6519, 780.0)


def test_origin_maps_to_self():
    lat, lon, alt = ned_to_wgs84(0, 0, 0, *ORIGIN)
    assert abs(lat - ORIGIN[0]) < 1e-6
    assert abs(lon - ORIGIN[1]) < 1e-6
    assert abs(alt - ORIGIN[2]) < 0.1


def test_round_trip():
    x, y, z = 100.0, -200.0, -50.0  # 100 m North, 200 m West, 50 m up
    lat, lon, alt = ned_to_wgs84(x, y, z, *ORIGIN)
    xr, yr, zr = wgs84_to_ned(lat, lon, alt, *ORIGIN)
    assert abs(xr - x) < 0.01
    assert abs(yr - y) < 0.01
    assert abs(zr - z) < 0.01


def test_north_increases_latitude():
    lat0, _, _ = ned_to_wgs84(0, 0, 0, *ORIGIN)
    lat1, _, _ = ned_to_wgs84(1000, 0, 0, *ORIGIN)
    assert lat1 > lat0


def test_east_increases_longitude():
    _, lon0, _ = ned_to_wgs84(0, 0, 0, *ORIGIN)
    _, lon1, _ = ned_to_wgs84(0, 1000, 0, *ORIGIN)
    assert lon1 > lon0
