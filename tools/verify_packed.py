"""比对「打包进 exe 的字节码」与「当前源码编译出的字节码」，证明包里是哪个版本。

用法: python .runtime/verify_packed.py [EXE]
规范化：丢掉行号、RESUME/CACHE/NOP 等无意义指令，只比指令序列与常量。
"""
from __future__ import annotations

import dis
import hashlib
import io
import sys
import types
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

REPO = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

MODULES = ["localgen.desktop", "localgen.desktop.client", "localgen.engine.sdcpp",
           "localgen.engine.moss", "localgen.jobs.service", "localgen.storage.images",
           "localgen.config", "localgen.api.app", "localgen.jobs.scheduler"]
SKIP = {"RESUME", "CACHE", "NOP", "EXTENDED_ARG", "PRECALL"}


def norm(co):
    out = []
    for ins in dis.get_instructions(co):
        if ins.opname in SKIP:
            continue
        arg = ins.argval
        if isinstance(arg, types.CodeType):
            arg = f"<code:{arg.co_name}>"
        elif not isinstance(arg, (str, int, float, bytes, type(None), bool)):
            arg = f"<{type(arg).__name__}>"
        out.append((ins.opname, arg))
    return out


def source_of(module):
    base = REPO / "src" / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def main():
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "dist" / "LocalGen" / "LocalGen.exe"
    print(f"exe: {exe}")
    print(f"exe mtime: {exe.stat().st_mtime_ns}  size {exe.stat().st_size}\n")
    arc = CArchiveReader(str(exe))
    blob = arc.extract("PYZ.pyz")
    # 临时文件按 exe 路径哈希命名：两个进程并发校验不同 exe 时不再互踩同一文件
    # （曾经固定叫 PYZ.pyz，并行构建 + 验收时读到对方写了一半的内容，误报 DIFF）。
    tag = hashlib.sha256(str(exe.resolve()).encode()).hexdigest()[:12]
    tmp = REPO / ".runtime" / f"PYZ-{tag}.pyz"
    tmp.write_bytes(blob)
    z = ZlibArchiveReader(str(tmp))

    same = diff = missing = 0
    for module in MODULES:
        source = source_of(module)
        if source is None:
            print(f"MISSING-SRC {module}")
            missing += 1
            continue
        try:
            packed = z.extract(module)
        except KeyError:
            print(f"MISSING-PYZ {module}")
            missing += 1
            continue
        live = compile(source.read_text("utf-8"), str(source), "exec", dont_inherit=True)
        a, b = norm(packed), norm(live)
        if a == b:
            print(f"SAME  {module:32s} {len(a):5d} ins")
            same += 1
        else:
            first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            print(f"DIFF  {module:32s} packed {len(a)} vs live {len(b)}  first diff @ {first}")
            print(f"        packed: {a[first] if first < len(a) else '<end>'}")
            print(f"        live  : {b[first] if first < len(b) else '<end>'}")
            diff += 1
    print(f"\nSAME {same}  DIFF {diff}  MISSING {missing}")
    return 0 if diff == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
