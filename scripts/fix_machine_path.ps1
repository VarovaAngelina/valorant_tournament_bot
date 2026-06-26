# Запустите этот скрипт от имени администратора:
# PowerShell -> ПКМ -> "Запуск от имени администратора"
#   Set-ExecutionPolicy -Scope Process Bypass
#   & "C:\Users\angel\valorant_tournament_bot\scripts\fix_machine_path.ps1"

$raw = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if (-not $raw) {
    Write-Error 'Machine PATH is empty.'
    exit 1
}

$parts = $raw -split ';' | ForEach-Object { $_.Trim().Trim('"') } | Where-Object { $_ -and $_ -ne '"' }
$clean = ($parts | Select-Object -Unique) -join ';'

Write-Host 'Было:'
Write-Host $raw
Write-Host ''
Write-Host 'Станет:'
Write-Host $clean
Write-Host ''

[Environment]::SetEnvironmentVariable('Path', $clean, 'Machine')
Write-Host 'Machine PATH исправлен. Перезапустите Cursor и все терминалы.'
