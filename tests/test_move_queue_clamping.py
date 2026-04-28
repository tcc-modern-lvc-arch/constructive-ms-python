"""DroneMove clamping stays within safety limits."""
from constructive_airsim_ms.agent.llm_policy import DroneMove, _clamp_move
from constructive_airsim_ms.config import settings


def test_speed_clamped():
    m = _clamp_move(DroneMove(vx=999, vy=-999, vz=0, yaw_rate=0, duration_ms=500))
    assert abs(m.vx) <= settings.max_speed_ms
    assert abs(m.vy) <= settings.max_speed_ms


def test_duration_clamped():
    m = _clamp_move(DroneMove(vx=0, vy=0, vz=0, yaw_rate=0, duration_ms=99999))
    assert m.duration_ms <= 5000

    m2 = _clamp_move(DroneMove(vx=0, vy=0, vz=0, yaw_rate=0, duration_ms=1))
    assert m2.duration_ms >= 500


def test_yaw_rate_clamped():
    m = _clamp_move(DroneMove(vx=0, vy=0, vz=0, yaw_rate=360, duration_ms=500))
    assert abs(m.yaw_rate) <= 90
