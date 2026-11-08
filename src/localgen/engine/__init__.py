from dataclasses import dataclass


@dataclass(frozen=True)
class EngineStatus:
    state: str = "stopped"
    code: str = "ENGINE_STOPPED"
    device: str | None = None


@dataclass
class Observation:
    image: bytes | None = None
    progress: tuple[int, int] | None = None
