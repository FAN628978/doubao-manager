$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildVenv = Join-Path $projectDir ".build-venv"
$pythonExe = Join-Path $buildVenv "Scripts\python.exe"
$distDir = Join-Path $projectDir "dist"
$packageName = "Doubao-Account-Desk-Windows-x64"
$packageDir = Join-Path $distDir $packageName
$packageDataDir = Join-Path $packageDir "data"
$zipPath = Join-Path $distDir "$packageName.zip"
$preserveRoot = Join-Path $projectDir (".tmp\portable-data-" + [guid]::NewGuid().ToString("N"))
$preservedDataDir = Join-Path $preserveRoot "data"
$hasPreservedData = $false

Set-Location $projectDir

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found in PATH."
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    uv venv $buildVenv --python 3.12
}

uv pip install --python $pythonExe -r requirements-build.txt

if (Test-Path -LiteralPath $packageDataDir) {
    New-Item -ItemType Directory -Path $preserveRoot -Force | Out-Null
    Move-Item -LiteralPath $packageDataDir -Destination $preservedDataDir
    $hasPreservedData = $true
}

try {
    & $pythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --name $packageName `
        --add-data "static;static" `
        --collect-all playwright `
        portable_main.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    # The release ZIP contains an empty data directory. Existing private account data
    # is restored only to the local portable folder after compression.
    New-Item -ItemType Directory -Path $packageDataDir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $packageDir "README.txt") -Encoding UTF8 -Value @"
Doubao Account Desk Portable

1. Extract the entire ZIP. Do not copy the EXE alone.
2. Double-click $packageName.exe. The manager will open in your default browser.
3. Keep the command window open. Close it or press Ctrl+C to stop the program.
4. Account sessions, the database, and downloaded videos are stored in the data folder.
5. To upgrade, extract the new ZIP to a new folder, close the old program, then copy the entire old data folder into the new extracted folder and replace its empty data folder.
6. This package supports 64-bit Windows 10/11 and requires Google Chrome.

The data folder contains private account data. Do not share a used data folder.
All persisted file paths are relative to this executable and confined to its data folder.
"@

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -LiteralPath $packageDir -DestinationPath $zipPath -CompressionLevel Optimal
}
finally {
    if ($hasPreservedData) {
        if (Test-Path -LiteralPath $packageDataDir) {
            Remove-Item -LiteralPath $packageDataDir -Recurse -Force
        }
        New-Item -ItemType Directory -Path $packageDir -Force | Out-Null
        Move-Item -LiteralPath $preservedDataDir -Destination $packageDataDir
        Remove-Item -LiteralPath $preserveRoot -Force
    }
}

Write-Host "Portable folder: $packageDir"
Write-Host "Distribution ZIP: $zipPath"
if ($hasPreservedData) {
    Write-Host "Existing portable data preserved: $packageDataDir"
}
