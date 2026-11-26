param([ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version)

$ErrorActionPreference = "Stop"

function Get-InstalledVersion {
    try {
        $line = & agent-browser --version 2>$null | Select-Object -First 1
        if ($line -match '(\d+\.\d+\.\d+)') { return $Matches[1] }
    }
    catch { }
    return $null
}

$installed = Get-InstalledVersion
$targetVersion = if ($Version) { $Version } elseif ($installed) { $installed } else { '0.44.0' }

if ($installed -ne $targetVersion) {
    npm i -g "agent-browser@$targetVersion"
    if ($LASTEXITCODE -ne 0) { throw "agent-browser installation failed." }
}
else {
    Write-Host "Using installed agent-browser $installed."
}

$npmPrefix = & npm prefix -g
if ($npmPrefix -and (-not ($env:Path -split ';' -contains $npmPrefix))) {
    $env:Path = "$npmPrefix;$env:Path"
}

if ((Get-InstalledVersion) -ne $targetVersion) {
    throw "agent-browser still not available after install. Check npm global bin is on PATH."
}

Write-Host "Installing browser runtime ..."
& agent-browser install
if ($LASTEXITCODE -ne 0) { throw "Browser runtime installation failed." }

Write-Host "agent-browser ready: $(& agent-browser --version)"
