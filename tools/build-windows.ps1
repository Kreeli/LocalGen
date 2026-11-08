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
    Get-Item -LiteralPath (Join-Path $bundle 'LocalGen.exe')
} finally {
    Pop-Location
}
