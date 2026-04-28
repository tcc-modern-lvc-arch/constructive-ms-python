"""Pre-scripted bootstrap route flown before the LLM generates its first plan.

Route: ascend to ~48 m AGL → N 80 m → E 80 m → S 80 m → W 80 m → hold (~86 s total).
Gives the LLM comfortable time to warm up before taking control.

DroneMove convention: vz positive = up (client.py negates before AirSim NED call).
"""
from constructive_airsim_ms.agent.llm_policy import DroneMove

DEFAULT_PLAN_SUMMARY = (
    "Ascend to 48 m AGL over Mackenzie Higienópolis campus (São Paulo), "
    "fly a square loop ~80 m per side at 5 m/s, return to origin, hold position."
)

_M = lambda vx, vy, vz: DroneMove(vx=vx, vy=vy, vz=vz, yaw_rate=0.0, duration_ms=2000)


def traced_route() -> list[DroneMove]:
    return [
        # Ascend ~48 m at 4 m/s (6 × 2 s)
        *[_M(0.0,   0.0,  4.0)] * 6,
        # Fly north ~80 m at 5 m/s (8 × 2 s)
        *[_M(5.0,   0.0,  0.0)] * 8,
        # Fly east ~80 m at 5 m/s (8 × 2 s)
        *[_M(0.0,   5.0,  0.0)] * 8,
        # Fly south ~80 m at 5 m/s — returns to start longitude (8 × 2 s)
        *[_M(-5.0,  0.0,  0.0)] * 8,
        # Fly west ~80 m at 5 m/s — returns to origin (8 × 2 s)
        *[_M(0.0,  -5.0,  0.0)] * 8,
        # Hold position while first LLM plan loads (5 × 2 s)
        *[_M(0.0,   0.0,  0.0)] * 5,
    ]
