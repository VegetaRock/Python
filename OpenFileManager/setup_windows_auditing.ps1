# Run from an elevated PowerShell window.
# Example:
#   powershell -ExecutionPolicy Bypass -File .\setup_windows_auditing.ps1 -Paths "D:\Projects","E:\CAD","C:\Users\Public\Documents"

#requires -RunAsAdministrator
param(
    [string[]]$Paths = @("$env:USERPROFILE\Documents", "$env:USERPROFILE\Desktop")
)

Write-Host "Enabling Windows Audit File System success auditing..."
auditpol /set /subcategory:"File System" /success:enable | Out-Host

# S-1-1-0 = Everyone. This avoids localization problems with the text name "Everyone".
$identitySid = New-Object System.Security.Principal.SecurityIdentifier("S-1-1-0")
$identity = $identitySid.Translate([System.Security.Principal.NTAccount])

$rights = [System.Security.AccessControl.FileSystemRights]"ReadData, WriteData, AppendData, CreateFiles, CreateDirectories, ReadAttributes, WriteAttributes, Delete"
$inheritance = [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
$propagation = [System.Security.AccessControl.PropagationFlags]"None"
$auditFlags = [System.Security.AccessControl.AuditFlags]"Success"

foreach ($path in $Paths) {
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Warning "Path does not exist, skipping: $path"
        continue
    }

    Write-Host "Adding success audit rule to: $path"
    $acl = Get-Acl -LiteralPath $path
    $rule = New-Object System.Security.AccessControl.FileSystemAuditRule(
        $identity,
        $rights,
        $inheritance,
        $propagation,
        $auditFlags
    )
    $acl.AddAuditRule($rule)
    Set-Acl -LiteralPath $path -AclObject $acl
}

Write-Host "Done. Verify with: auditpol /get /subcategory:`"File System`""
