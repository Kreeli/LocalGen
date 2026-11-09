param([string]$DistPath = 'dist')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Create the LocalGen .venv first. See docs/windows-setup.md.'
    }
    & $python -m PyInstaller --noconfirm --onedir --windowed --name LocalGen --distpath $DistPath `
        --add-data 'config\default.toml:config' --add-data 'workflows:workflows' `
        --hidden-import uvicorn.loops.auto --hidden-import uvicorn.loops.asyncio `
        --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.http.h11_impl `
        --hidden-import uvicorn.lifespan.on src\localgen\__main__.py
    if ($LASTEXITCODE -ne 0) { throw 'LocalGen build failed.' }
    # Python's older app-local MSVC DLLs load before Qt. Use the runtime shipped
    # with the pinned PySide6 wheel for all modules in the packaged process.
    $qt = Join-Path $root '.venv\Lib\site-packages\PySide6'
    $bundle = Join-Path (Resolve-Path -LiteralPath $DistPath).Path 'LocalGen'
    $internal = Join-Path $bundle '_internal'
    foreach ($dll in @('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll',
                       'msvcp140_1.dll', 'msvcp140_2.dll', 'concrt140.dll')) {
        Copy-Item -LiteralPath (Join-Path $qt $dll) -Destination (Join-Path $internal $dll) -Force
    }
    # Qt uses the Windows 11 ICU API. PyInstaller can pick up an incompatible
    # ICU with version-suffixed exports from Git or another PATH entry.
    Get-ChildItem -LiteralPath $internal -File -Filter 'icu*.dll' |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName }
    # 时间戳只能证明 exe 比源码新，不能证明代码真被装进去（stale build 目录 /
    # .pyc 缓存 / --exclude 都能让时间戳对、内容错）。这里从 exe 里挖出 PYZ 的
    # 字节码，跟当前 src/ 逐条指令比对；不一致就直接失败，别让"源码改了没重打"
    # 这类事故流到验收阶段。
    #
    # 注意 $LASTEXITCODE 的坑：上面几行 `Get-ChildItem | ForEach-Object` 不会更新它，
    # 但它可能还留着 PyInstaller 那次的值；而 `& $python` 之后要立刻取，中间不能插
    # 别的原生命令。这里显式接住退出码，避免误判。
    $exe = Join-Path $bundle 'LocalGen.exe'
    $LASTEXITCODE = 0
    & $python (Join-Path $root '.runtime\verify_packed.py') $exe
    $verifyCode = $LASTEXITCODE
    if ($verifyCode -ne 0) {
        throw "Packed bytecode does not match src/ (verify_packed exit $verifyCode)."
    }
    Get-Item -LiteralPath $exe
} finally {
    Pop-Location
}
