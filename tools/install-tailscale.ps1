# Trigger a UAC-elevated install of Tailscale.
#
# Why this file exists: our shell runs UNELEVATED, so msiexec gets blocked by
# UAC (observed exit codes 67 and 86). Elevation can only be granted through a
# UAC prompt, and only a human can click it -- so this script's whole job is to
# raise that prompt, sparing the user from hunting for the file and using the
# right-click menu.
#
# Keep this file ASCII-only: Chinese text in scripts that Windows shells parse
# (batch files especially) turns into garbage under a GBK code page.

$msi = "D:\EY-project\tools\tailscale-setup-amd64.msi"

if (-not (Test-Path $msi)) {
    Write-Host "MSI not found: $msi"
    exit 1
}

Write-Host "Raising UAC prompt... please click Yes in the dialog."
Start-Process -FilePath "msiexec.exe" `
    -ArgumentList @("/i", "`"$msi`"", "/passive", "/norestart") `
    -Verb RunAs
Write-Host "Prompt sent. The installer runs silently after you approve."
