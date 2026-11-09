"""在桌面（和开始菜单）创建 LocalGen 的快捷方式。

用法：
    python tools/make_shortcut.py                # 桌面 + 开始菜单
    python tools/make_shortcut.py --desktop-only

目标取 `dist\\LocalGen\\LocalGen.exe`（打包版），不存在时回退到源码态启动命令。
图标用 `assets/localgen.ico`（由 tools/make_icon.py 生成，多尺寸 16~256）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "LocalGen" / "LocalGen.exe"
ICON = ROOT / "assets" / "localgen.ico"
NAME = "LocalGen"


def desktop_dir():
    """桌面路径 —— 可能是 OneDrive 重定向过的，不能硬编码 %USERPROFILE%\\Desktop。"""
    if os.name != "nt":
        return Path.home() / "Desktop"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, timeout=30,
    )
    folder = result.stdout.strip()
    return Path(folder) if folder else Path.home() / "Desktop"


def start_menu_dir():
    if os.name != "nt":
        return None
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Programs')"],
        capture_output=True, text=True, timeout=30,
    )
    folder = result.stdout.strip()
    return Path(folder) if folder else None


def create(folder: Path, target: Path, arguments: str, workdir: Path, icon: Path | None):
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / f"{NAME}.lnk"
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LINK_PATH);"
        f"$s.TargetPath=$env:TARGET_PATH;"
        f"$s.Arguments=$env:TARGET_ARGS;"
        f"$s.WorkingDirectory=$env:TARGET_CWD;"
        f"$s.IconLocation=$env:ICON_PATH;"
        "$s.Description='LocalGen · 本地生图工作台';"
        "$s.WindowStyle=1;"
        "$s.Save()"
    )
    environment = os.environ.copy()
    environment.update({
        "LINK_PATH": str(link),
        "TARGET_PATH": str(target),
        "TARGET_ARGS": arguments,
        "TARGET_CWD": str(workdir),
        "ICON_PATH": f"{icon},0" if icon else "",
    })
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, env=environment, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "创建快捷方式失败")
    return link


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop-only", action="store_true")
    args = parser.parse_args()

    if EXE.is_file():
        target, arguments, workdir = EXE, "", EXE.parent
    else:
        pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw.is_file():
            print("找不到打包版 exe，也找不到 .venv\\Scripts\\pythonw.exe", file=sys.stderr)
            return 1
        target, arguments, workdir = pythonw, "-m localgen", ROOT

    icon = ICON if ICON.is_file() else None
    made = [create(desktop_dir(), target, arguments, workdir, icon)]
    if not args.desktop_only:
        programs = start_menu_dir()
        if programs:
            made.append(create(programs, target, arguments, workdir, icon))

    print(f"目标  {target}")
    print(f"参数  {arguments or '(无)'}")
    print(f"图标  {icon}")
    for link in made:
        print(f"已创建 {link}  ({link.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
