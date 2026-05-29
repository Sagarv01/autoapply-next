# Codesign a built AutoApply Next .exe with signtool. Supports a local cert by SHA1 thumbprint and an SSL.com eSigner cloud HSM branch. Degrades to "unsigned" when secrets are missing so CI never hard-fails on a fork.
<#
.SYNOPSIS
    Sign a Windows executable (or PyInstaller onedir bundle) for AutoApply Next.

.DESCRIPTION
    Reads env vars:
        WIN_SIGNING_CERT_THUMBPRINT     SHA1 thumbprint of cert in CurrentUser\My
        WIN_TS_URL                      Optional. Defaults to http://timestamp.digicert.com
        WIN_SIGNING_USE_ESIGNER         If "1", use SSL.com eSigner KSP (cloud HSM)
                                        instead of a local cert
        ESIGNER_USERNAME                Required when WIN_SIGNING_USE_ESIGNER=1
        ESIGNER_PASSWORD                Required when WIN_SIGNING_USE_ESIGNER=1
        ESIGNER_TOTP_SECRET             Required when WIN_SIGNING_USE_ESIGNER=1
        ESIGNER_CREDENTIAL_ID           Required when WIN_SIGNING_USE_ESIGNER=1

    Missing required vars -> prints "MISSING_CERTS: <list>" and exits 0
    (leaves unsigned exe in place so CI can still upload a dev artefact).

.PARAMETER ExePath
    Absolute path to the .exe to sign.
#>

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string] $ExePath
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Error "ERROR: exe not found: $ExePath"
    exit 2
}

# Resolve to an absolute path so error messages match the spec contract.
$ExePath = (Resolve-Path -LiteralPath $ExePath).Path

$TS = $env:WIN_TS_URL
if ([string]::IsNullOrWhiteSpace($TS)) {
    $TS = "http://timestamp.digicert.com"
}

# ---------------------------------------------------------------------------
# Locate signtool. On GH Actions windows-latest it ships with the Windows SDK
# but is not on PATH. We try PATH first then walk known SDK locations.
# ---------------------------------------------------------------------------
function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe",
        "C:\Program Files (x86)\Windows Kits\10\App Certification Kit\signtool.exe",
        "C:\Program Files\Windows Kits\10\bin\*\x64\signtool.exe"
    )
    foreach ($pattern in $candidates) {
        $found = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
                 Sort-Object -Property FullName -Descending |
                 Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$signtool = Find-SignTool
if (-not $signtool) {
    Write-Host "MISSING_CERTS: signtool.exe (Windows SDK not installed)"
    Write-Host "Leaving unsigned exe in place at $ExePath"
    exit 0
}

# ---------------------------------------------------------------------------
# Branch: SSL.com eSigner cloud HSM
# ---------------------------------------------------------------------------
if ($env:WIN_SIGNING_USE_ESIGNER -eq "1") {
    $required = @("ESIGNER_USERNAME", "ESIGNER_PASSWORD", "ESIGNER_TOTP_SECRET", "ESIGNER_CREDENTIAL_ID")
    $missing = @()
    foreach ($name in $required) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            $missing += $name
        }
    }

    # eSignerKSP.dll path. SSL.com's installer puts it under Program Files.
    $kspDll = "C:\Program Files\SSL.com\eSignerKSP\eSignerKSP.dll"
    if (-not (Test-Path -LiteralPath $kspDll)) {
        $missing += "eSignerKSP.dll (not found at $kspDll, install SSL.com eSigner KSP first)"
    }

    if ($missing.Count -gt 0) {
        Write-Host ("MISSING_CERTS: " + ($missing -join ", "))
        Write-Host "Leaving unsigned exe in place at $ExePath"
        exit 0
    }

    Write-Host "Signing via SSL.com eSigner cloud HSM: $ExePath"
    & $signtool sign `
        /fd SHA256 `
        /tr $TS `
        /td SHA256 `
        /csp "eSignerKSP" `
        /kc "[$($env:ESIGNER_CREDENTIAL_ID)]" `
        $ExePath

    if ($LASTEXITCODE -ne 0) {
        Write-Error "signtool exited with code $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}
else {
    # -----------------------------------------------------------------------
    # Branch: local cert in CurrentUser\My identified by SHA1 thumbprint
    # -----------------------------------------------------------------------
    $thumbprint = $env:WIN_SIGNING_CERT_THUMBPRINT
    if ([string]::IsNullOrWhiteSpace($thumbprint)) {
        Write-Host "MISSING_CERTS: WIN_SIGNING_CERT_THUMBPRINT"
        Write-Host "Leaving unsigned exe in place at $ExePath"
        exit 0
    }

    # Strip spaces that PowerShell's certificate UI sometimes inserts.
    $thumbprint = $thumbprint -replace '\s', ''

    Write-Host "Signing $ExePath with cert thumbprint $thumbprint"
    & $signtool sign `
        /fd SHA256 `
        /tr $TS `
        /td SHA256 `
        /sha1 $thumbprint `
        $ExePath

    if ($LASTEXITCODE -ne 0) {
        Write-Error "signtool exited with code $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
Write-Host "Verifying signature"
& $signtool verify /pa /v $ExePath
if ($LASTEXITCODE -ne 0) {
    Write-Error "signtool verify exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host "DONE: signed $ExePath"
