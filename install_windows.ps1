param([ValidateSet('Ask', 'Yes', 'No')][string]$AutoStart = 'Ask')

$ErrorActionPreference = 'Stop'

function Get-AutoStartChoice {
    param([string]$Choice, [bool]$CurrentlyEnabled)
    if ($Choice -eq 'Yes') { return $true }
    if ($Choice -eq 'No') { return $false }
    $defaultAnswer = if ($CurrentlyEnabled) { 'O' } else { 'N' }
    do {
        $answer = (Read-Host "Lancer LED Meter automatiquement a l'ouverture de session Windows ? [O/N, defaut $defaultAnswer]").Trim().ToUpperInvariant()
        if (-not $answer) { return $CurrentlyEnabled }
        if ($answer -in @('O', 'OUI', 'Y', 'YES')) { return $true }
        if ($answer -in @('N', 'NON', 'NO')) { return $false }
        Write-Host 'Reponds O pour oui ou N pour non.'
    } while ($true)
}

$startup = [Environment]::GetFolderPath('Startup')
$startupShortcut = Join-Path $startup 'LED Meter.lnk'
$enableAutoStart = Get-AutoStartChoice -Choice $AutoStart -CurrentlyEnabled (Test-Path -LiteralPath $startupShortcut)
$sourceDir = $PSScriptRoot
$installDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'LED Meter'
New-Item -ItemType Directory -Path $installDir -Force | Out-Null

foreach ($name in @('claude_meter.py', 'spotify_meter.py', 'windows_runner.py', 'requirements.txt', '.env.example', 'Lancer.bat', 'Installer-Windows.bat', 'install_windows.ps1')) {
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
$pythonw = Join-Path $installDir '.venv\Scripts\pythonw.exe'
$backgroundArgs = '"' + (Join-Path $installDir 'windows_runner.py') + '" --background'
if ($enableAutoStart) {
    $shortcut = $shell.CreateShortcut($startupShortcut)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = $backgroundArgs
    $shortcut.WorkingDirectory = $installDir
    $shortcut.Description = 'LED Meter - lancement automatique'
    $shortcut.Save()
} elseif (Test-Path -LiteralPath $startupShortcut) {
    Remove-Item -LiteralPath $startupShortcut
}

$desktop = [Environment]::GetFolderPath('Desktop')
$panelShortcut = $shell.CreateShortcut((Join-Path $desktop 'LED Meter.lnk'))
$panelShortcut.TargetPath = $pythonw
$panelShortcut.Arguments = '"' + (Join-Path $installDir 'windows_runner.py') + '" --background --open'
$panelShortcut.WorkingDirectory = $installDir
$panelShortcut.Description = 'Ouvrir LED Meter'
$panelShortcut.Save()

Start-Process -FilePath $pythonw -ArgumentList $backgroundArgs -WorkingDirectory $installDir -WindowStyle Hidden
Write-Host "Installe dans $installDir"
if ($enableAutoStart) {
    Write-Host 'Demarrage automatique active pour ta session Windows.'
} else {
    Write-Host 'Demarrage automatique desactive. Le raccourci du Bureau reste disponible.'
}
Write-Host 'Utilise le raccourci LED Meter sur le Bureau pour ouvrir le panneau.'
Write-Host "Pour desactiver le demarrage automatique : retire LED Meter.lnk de $startup"
