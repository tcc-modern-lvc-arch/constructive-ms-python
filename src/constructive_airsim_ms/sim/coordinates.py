"""NED (AirSim local) ↔ WGS84 using Mackenzie SP as origin."""
import pymap3d


def ned_to_wgs84(
    x_m: float, y_m: float, z_m: float,
    origin_lat: float, origin_lon: float, origin_alt: float,
) -> tuple[float, float, float]:
    """AirSim NED (x=North, y=East, z=Down) → (lat, lon, alt_m)."""
    lat, lon, alt = pymap3d.ned2geodetic(
        x_m, y_m, -z_m,          # pymap3d uses alt=up, AirSim z=down
        origin_lat, origin_lon, origin_alt,
    )
    return float(lat), float(lon), float(alt)


def wgs84_to_ned(
    lat: float, lon: float, alt: float,
    origin_lat: float, origin_lon: float, origin_alt: float,
) -> tuple[float, float, float]:
    """(lat, lon, alt_m) → AirSim NED (x=North, y=East, z=Down)."""
    n, e, d = pymap3d.geodetic2ned(
        lat, lon, alt,
        origin_lat, origin_lon, origin_alt,
    )
    return float(n), float(e), float(-d)
