"""Guard against accidental method loss when refactoring the engine classes."""
from localgen.engine.comfy import ComfyEngine
from localgen.engine.mock import MockEngine


def test_comfy_engine_surface_intact():
    for name in ("start", "tick", "boot", "stop_process", "recycle", "shutdown", "submit",
                 "submit_sdcpp", "observe", "observe_sdcpp", "queue", "cancel", "cleanup",
                 "missing", "get", "post"):
        assert callable(getattr(ComfyEngine, name, None)), f"ComfyEngine.{name} missing"


def test_mock_engine_surface_intact():
    for name in ("start", "tick", "shutdown", "recycle", "submit", "observe", "cancel", "cleanup", "missing"):
        assert callable(getattr(MockEngine, name, None)), f"MockEngine.{name} missing"
