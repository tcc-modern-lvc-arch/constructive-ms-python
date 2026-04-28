"""Synchronous AirSim MultirotorClient wrapper. Runs inside AirSimControlThread only."""
import socket
import time

import airsim
import structlog

from constructive_airsim_ms.config import settings

log = structlog.get_logger()


def _port_ready(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True if AirSim's TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class DroneClient:
    def __init__(self) -> None:
        # timeout_value: per-RPC timeout in seconds (AirSim default is 3600 — hangs forever).
        # 10 s lets confirmConnection() fail fast during startup retries.
        self._client = airsim.MultirotorClient(
            ip=settings.airsim_host,
            port=settings.airsim_port,
            timeout_value=10,
        )

    def connect(self, max_retries: int = 30, retry_delay: float = 3.0) -> None:
        log.info("airsim_connecting", host=settings.airsim_host, port=settings.airsim_port)
        for attempt in range(1, max_retries + 1):
            log.info("airsim_connect_attempt", attempt=attempt, max=max_retries)

            if not _port_ready(settings.airsim_host, settings.airsim_port):
                log.warning("airsim_port_not_ready", attempt=attempt)
                time.sleep(retry_delay)
                continue

            try:
                self._client.confirmConnection()
                self._client.enableApiControl(True, settings.drone_id)
                self._client.armDisarm(True, settings.drone_id)
                log.info("airsim_connected", drone=settings.drone_id, attempt=attempt)
                return
            except Exception as exc:
                log.warning("airsim_connect_retry", attempt=attempt,
                            max=max_retries, error=repr(exc))
                if attempt == max_retries:
                    raise
                time.sleep(retry_delay)

        raise ConnectionError(f"Could not connect to AirSim after {max_retries} attempts")

    def takeoff(self) -> None:
        self._client.takeoffAsync(vehicle_name=settings.drone_id).join()
        log.info("takeoff_complete")

    def reset(self) -> None:
        self._client.reset()
        time.sleep(0.5)
        self._client.enableApiControl(True, settings.drone_id)
        self._client.armDisarm(True, settings.drone_id)
        log.info("airsim_reset")

    def get_state(self) -> airsim.MultirotorState:
        return self._client.getMultirotorState(vehicle_name=settings.drone_id)

    def get_collision(self) -> airsim.CollisionInfo:
        return self._client.simGetCollisionInfo(vehicle_name=settings.drone_id)

    def move_by_velocity(
        self, vx: float, vy: float, vz: float, duration_s: float, yaw_rate: float = 0.0
    ) -> None:
        # DroneMove convention: positive vz = up. AirSim NED: positive z = down → negate.
        self._client.moveByVelocityAsync(
            vx, vy, -vz,
            duration_s,
            drivetrain=airsim.DrivetrainType.MaxDegreeOfFreedom,
            yaw_mode=airsim.YawMode(is_rate=True, yaw_or_rate=yaw_rate),
            vehicle_name=settings.drone_id,
        )

    def get_images(self) -> list[airsim.ImageResponse]:
        return self._client.simGetImages(
            [airsim.ImageRequest("front_center", airsim.ImageType.DepthPerspective, True, False)],
            vehicle_name=settings.drone_id,
        )

    def get_scene_image(self) -> bytes:
        """Capture front_center Scene image, return PNG-compressed bytes."""
        responses = self._client.simGetImages(
            [airsim.ImageRequest("front_center", airsim.ImageType.Scene, False, True)],
            vehicle_name=settings.drone_id,
        )
        if not responses or not responses[0].image_data_uint8:
            return b""
        return bytes(responses[0].image_data_uint8)

    def move_to_position(self, x: float, y: float, z: float, velocity: float) -> None:
        """Non-blocking: AirSim position controller flies to NED (x, y, z) at given velocity.
        z is AirSim NED convention (negative = up)."""
        self._client.moveToPositionAsync(
            x, y, z, velocity,
            vehicle_name=settings.drone_id,
        )

    def hover(self) -> None:
        self._client.hoverAsync(vehicle_name=settings.drone_id)
