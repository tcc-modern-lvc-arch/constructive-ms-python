"""Build structured obstacle observations from AirSim depth image (synchronous)."""
from __future__ import annotations

import numpy as np

from constructive_airsim_ms.sim.client import DroneClient

_SECTORS         = ["far-left", "left", "center", "right", "far-right"]
_OBSTACLE_RANGE_M = 20.0


def get_nearby_obstacles_sync(client: DroneClient) -> list[dict]:
    """Return [{direction, distance_m}] for obstacles within 20 m. Runs in AirSim thread."""
    responses = client.get_images()
    if not responses or len(responses[0].image_data_float) == 0:
        return []

    r     = responses[0]
    depth = np.array(r.image_data_float, dtype=np.float32).reshape(r.height, r.width)
    depth = np.nan_to_num(depth, nan=_OBSTACLE_RANGE_M, posinf=_OBSTACLE_RANGE_M)

    obstacles = []
    for label, sector in zip(_SECTORS, np.array_split(depth, len(_SECTORS), axis=1)):
        min_dist = float(np.min(sector))
        if min_dist < _OBSTACLE_RANGE_M:
            obstacles.append({"direction": label, "distance_m": round(min_dist, 1)})
    return obstacles
