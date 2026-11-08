import hashlib

from localgen.engine import gguf


def test_gguf_loader_requires_reviewed_sources(tmp_path, monkeypatch):
    content = b"# reviewed source\n"
    monkeypatch.setattr(gguf, "SOURCES", {"__init__.py": hashlib.sha256(content).hexdigest()})
    assert not gguf.verified_loader(tmp_path)

    folder = tmp_path / "custom_nodes" / gguf.GGUF_FOLDER
    folder.mkdir(parents=True)
    source = folder / "__init__.py"
    source.write_bytes(content)
    assert gguf.verified_loader(tmp_path)

    source.write_bytes(b"# changed\n")
    assert not gguf.verified_loader(tmp_path)

    source.write_bytes(content)
    (folder / "extra.py").touch()
    assert not gguf.verified_loader(tmp_path)
