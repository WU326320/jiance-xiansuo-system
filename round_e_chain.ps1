$d = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $d
try { & (Join-Path $d 'wu_validate.ps1') -Worker *> (Join-Path $d 'logs\round_e_validate_console.txt') } catch { $_ | Out-File (Join-Path $d 'logs\round_e_validate_console.txt') -Append }
$ok = $true
try { & (Join-Path $d 'wu_tools\deploy_workbench.ps1') *> (Join-Path $d 'logs\round_e_deploy.txt') } catch { $ok = $false; $_ | Out-File (Join-Path $d 'logs\round_e_deploy.txt') -Append }
('deploy_ok=' + $ok) | Out-File (Join-Path $d 'logs\round_e_DONE.txt') -Encoding utf8