# Exercise installer startup choices without installing or touching user shortcuts.
$ErrorActionPreference = 'Stop'
$syntaxErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'install_windows.ps1'), [ref]$null, [ref]$syntaxErrors)
if ($syntaxErrors) { throw ($syntaxErrors | Out-String) }
$choiceFunction = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-AutoStartChoice'
}, $true)
Invoke-Expression $choiceFunction.Extent.Text

function Assert-Equal($actual, $expected, $label) {
    if ($actual -ne $expected) { throw "$label : expected $expected, got $actual" }
}
$script:answer = ''
function Read-Host { param($Prompt) return $script:answer }
Assert-Equal (Get-AutoStartChoice 'Yes' $false) $true 'Explicit enable'
Assert-Equal (Get-AutoStartChoice 'No' $true) $false 'Explicit disable'
Assert-Equal (Get-AutoStartChoice 'Ask' $true) $true 'Reinstall keeps enabled default'
Assert-Equal (Get-AutoStartChoice 'Ask' $false) $false 'Fresh install defaults to manual'
$script:answer = 'oui'
Assert-Equal (Get-AutoStartChoice 'Ask' $false) $true 'Interactive yes'
$script:answer = 'non'
Assert-Equal (Get-AutoStartChoice 'Ask' $true) $false 'Interactive no'

# Run the real startup branch in a disposable directory with a fake COM shortcut.
$startupBlock = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Clauses[0].Item1.Extent.Text -eq '$enableAutoStart' -and
    $node.Extent.Text.Contains('CreateShortcut')
}, $true)
if (-not $startupBlock) { throw 'Startup branch not found' }
$testDirectory = Join-Path $env:TEMP ('led-installer-test-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $testDirectory | Out-Null
try {
    $startupShortcut = Join-Path $testDirectory 'LED Meter.lnk'
    $otherFile = Join-Path $testDirectory 'unrelated.lnk'
    Set-Content -LiteralPath $otherFile -Value 'keep'
    $pythonw = 'C:\Test\pythonw.exe'
    $backgroundArgs = '"C:\Test\windows_runner.py" --background'
    $installDir = 'C:\Test'
    $shell = [pscustomobject]@{}
    $shell | Add-Member ScriptMethod CreateShortcut {
        param($path)
        $link = [pscustomobject]@{Path=$path; TargetPath=''; Arguments=''; WorkingDirectory=''; Description=''}
        $link | Add-Member ScriptMethod Save { Set-Content -LiteralPath $this.Path -Value ($this.TargetPath + ' ' + $this.Arguments) }
        return $link
    }
    $enableAutoStart = $true
    Invoke-Expression $startupBlock.Extent.Text
    Assert-Equal (Test-Path -LiteralPath $startupShortcut) $true 'Startup shortcut created'
    Assert-Equal ((Get-Content -LiteralPath $startupShortcut) -like '*--background') $true 'Background target'
    $enableAutoStart = $false
    Invoke-Expression $startupBlock.Extent.Text
    Assert-Equal (Test-Path -LiteralPath $startupShortcut) $false 'Startup shortcut removed'
    Assert-Equal (Get-Content -LiteralPath $otherFile) 'keep' 'Other shortcuts untouched'
} finally {
    # Delete only the two exact test files, then the empty directory.
    foreach ($file in @($startupShortcut, $otherFile)) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
    }
    Remove-Item -LiteralPath $testDirectory
}
Write-Host 'Windows installer startup choices: OK'
