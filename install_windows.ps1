$ErrorActionPreference = 'Stop'
$sourceDir = $PSScriptRoot
$installDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'LED Meter'
New-Item -ItemType Directory -Path $installDir -Force | Out-Null

foreach ($name in @('claude_meter.py', 'windows_runner.py', 'requirements.txt', '.env.example', 'Lancer.bat', 'Installer-Windows.bat', 'install_windows.ps1')) {
    $sourceFile = Join-Path $sourceDir $name
    $targetFile = Join-Path $installDir $name
    if ([IO.Path]::GetFullPath($sourceFile) -ne [IO.Path]::GetFullPath($targetFile)) {
        Copy-Item -LiteralPath $sourceFile -Destination $targetFile -Force
    }
}
$config = Join-Path $installDir '.env'
if (-not (Test-Path -LiteralPath $config)) {
    # New install uses its own credentials. Existing installed settings are preserved.
    Copy-Item -LiteralPath (Join-Path $installDir '.env.example') -Destination $config
}
$python = Join-Path $installDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Preparation de Python...'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv (Join-Path $installDir '.venv')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv (Join-Path $installDir '.venv')
    } else {
        throw 'Installe Python 3 depuis python.org, puis relance cet installateur.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Creation de l environnement Python impossible.' }
}
Write-Host 'Installation des dependances...'
& $python -m pip install -r (Join-Path $installDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installation des dependances impossible.' }

$shell = New-Object -ComObject WScript.Shell
$startup = [Environment]::GetFolderPath('Startup')
$shortcut = $shell.CreateShortcut((Join-Path $startup 'LED Meter.lnk'))
$shortcut.TargetPath = Join-Path $installDir '.venv\Scripts\pythonw.exe'
$shortcut.Arguments = '"' + (Join-Path $installDir 'windows_runner.py') + '" --background'
$shortcut.WorkingDirectory = $installDir
$shortcut.Description = 'LED Meter - lancement automatique'
$shortcut.Save()

$desktop = [Environment]::GetFolderPath('Desktop')
$panelShortcut = $shell.CreateShortcut((Join-Path $desktop 'LED Meter.lnk'))
$panelShortcut.TargetPath = $shortcut.TargetPath
$panelShortcut.Arguments = '"' + (Join-Path $installDir 'windows_runner.py') + '" --background --open'
$panelShortcut.WorkingDirectory = $installDir
$panelShortcut.Description = 'Ouvrir LED Meter'
$panelShortcut.Save()

Start-Process -FilePath $shortcut.TargetPath -ArgumentList $shortcut.Arguments -WorkingDirectory $installDir -WindowStyle Hidden
Write-Host "Installe dans $installDir"
Write-Host 'Demarrage automatique active pour ta session Windows.'
Write-Host 'Utilise le raccourci LED Meter sur le Bureau pour ouvrir le panneau.'
Write-Host "Pour desactiver le demarrage automatique : retire LED Meter.lnk de $startup"
