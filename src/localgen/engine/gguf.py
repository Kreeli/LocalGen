"""Only permit the reviewed GGUF loader in the managed ComfyUI instance."""
import hashlib
import logging
from pathlib import Path

from localgen.config import guarded

logger = logging.getLogger(__name__)
GGUF_FOLDER = "localgen_gguf"
# city96/ComfyUI-GGUF at fa48477de8643e70506676ea3d08969e646e3372
SOURCES = {
    "__init__.py": "7ba473d8254a2867534565bb0b2ae194f1c55d5c2c00e30b6dbaf76be7d7a6b7",
    "dequant.py": "8a7b6d01322719469d2a3b5edf8a932f484ed0a5e8df6b9cd0955c48669c3754",
    "loader.py": "3e9ea827bfd8146271b8c1860a07087b73e686652b32fd9fb7b1f0b52272b4c1",
    "nodes.py": "001e9841f8fdb5bb138f29d4f088433defd949e1d9ca61d3715c78c347f9e2a7",
    "ops.py": "b927e24a7b00c181e9511900fb5afde45464793cf460e1d39a5e736ba093ce64",
}


def verified_loader(engine_root: Path) -> bool:
    try:
        directory = guarded(engine_root / "custom_nodes" / GGUF_FOLDER)
        if not directory.exists():
            return False
        entries = {path.name for path in directory.iterdir()}
        if entries - (SOURCES.keys() | {"LICENSE", "__pycache__"}):
            raise ValueError("unexpected files")
        if all(hashlib.sha256(guarded(directory / name).read_bytes()).hexdigest() == digest
               for name, digest in SOURCES.items()):
            return True
    except (OSError, ValueError):
        pass
    logger.warning("GGUF_LOADER_UNVERIFIED")
    return False
