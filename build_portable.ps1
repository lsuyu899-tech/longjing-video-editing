$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$WorkspaceRoot = Split-Path (Split-Path $Root -Parent) -Parent
$Python = Join-Path $WorkspaceRoot "OpenMontage\.venv\Scripts\python.exe"
$FfmpegSrc = Join-Path $WorkspaceRoot "OpenMontage\.local\ffmpeg\ffmpeg-8.1.1-essentials_build\bin"
$BuildName = "LongjingVideoEditing"
$ReleaseRoot = Join-Path $Root "release"
$AppDir = Join-Path $ReleaseRoot "longjing-video-editing-v0.1.6-windows"
$ZipPath = Join-Path $ReleaseRoot "longjing-video-editing-v0.1.6-windows.zip"

if (!(Test-Path -LiteralPath $Python)) {
  throw "Python runtime was not found: $Python"
}

if (!(Test-Path -LiteralPath (Join-Path $FfmpegSrc "ffmpeg.exe"))) {
  throw "ffmpeg.exe was not found: $FfmpegSrc"
}

$OldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python -m pip show pyinstaller 1>$null 2>$null
$HasPyInstaller = $LASTEXITCODE -eq 0
$ErrorActionPreference = $OldErrorActionPreference
if (!$HasPyInstaller) {
  Write-Host "[INFO] Installing PyInstaller into the build environment..."
  & $Python -m pip install pyinstaller
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }
}

Write-Host "[INFO] Cleaning old build output..."
Remove-Item -LiteralPath (Join-Path $Root "build") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $Root "dist") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $ReleaseRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[INFO] Building executable..."
Push-Location $Root
try {
  & $Python -m PyInstaller `
    --noconfirm `
    --onedir `
    --windowed `
    --name $BuildName `
    --add-data "index.html;." `
    --add-data "styles.css;." `
    --add-data "app.js;." `
    --hidden-import tkinter `
    --hidden-import tkinter.filedialog `
    launcher.py
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}
finally {
  Pop-Location
}

Write-Host "[INFO] Assembling portable folder..."
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item -Path (Join-Path $Root "dist\$BuildName\*") -Destination $AppDir -Recurse -Force

$PortableFfmpeg = Join-Path $AppDir "ffmpeg"
New-Item -ItemType Directory -Force -Path $PortableFfmpeg | Out-Null
Copy-Item -LiteralPath (Join-Path $FfmpegSrc "ffmpeg.exe") -Destination (Join-Path $PortableFfmpeg "ffmpeg.exe") -Force
Copy-Item -LiteralPath (Join-Path $FfmpegSrc "ffprobe.exe") -Destination (Join-Path $PortableFfmpeg "ffprobe.exe") -Force
Copy-Item -LiteralPath (Join-Path $Root "README.txt") -Destination (Join-Path $AppDir "README.txt") -Force

Write-Host "[INFO] Creating zip package..."
Compress-Archive -Path $AppDir -DestinationPath $ZipPath -Force

Write-Host ""
Write-Host "[DONE] Portable package created:"
Write-Host $ZipPath
