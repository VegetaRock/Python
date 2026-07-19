<#
Run this from an elevated PowerShell window after enabling auditing on the folders
you want to monitor. This script does not connect to PostgreSQL.
#>
param(
    [ValidateSet('documents','all')]
    [string]$Mode = 'documents',

    [int]$LookbackSeconds = 300,

    [int]$Limit = 50
)

$ErrorActionPreference = 'Stop'
$env:FILE_LOGGER_MODE = $Mode
$env:FILE_LOGGER_LOOKBACK_SECONDS = [string]$LookbackSeconds
$env:FILE_LOGGER_PREVIEW_LIMIT = [string]$Limit

Write-Host "OpenFilesLogger no-database preview"
Write-Host "Mode: $Mode"
Write-Host "Lookback seconds: $LookbackSeconds"
Write-Host "Limit: $Limit"
Write-Host ""
Write-Host "Open a PDF, Office file, or CAD file inside an audited folder, then run this again if no rows appear."
Write-Host ""

python .\file_open_logger.py preview --limit $Limit
