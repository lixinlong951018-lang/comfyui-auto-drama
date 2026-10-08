param([Parameter(Mandatory)][ValidateSet('Start','Stop')][string]$Action,[switch]$NoBrowser)
$projectPath = Split-Path -Parent $PSScriptRoot
$arguments = @('-B', (Join-Path $projectPath 'console/windows_control.py'), $Action.ToLower(), '--ui')
if ($NoBrowser) { $arguments = @('-B', (Join-Path $projectPath 'console/windows_control.py'), $Action.ToLower(), '--no-browser') }
& python @arguments
exit $LASTEXITCODE
