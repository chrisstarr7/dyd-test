# Bootstrap agent-browser for dyd-test. Idempotent; safe to rerun.
$ErrorActionPreference = "Stop"

function Get-InstalledVersion {
    try {
        $line = & agent-browser --version 2>$null | Select-Object -First 1
        if ($line -match '(\d+\.\d+\.\d+)') { return $Matches[1] }
    }
    catch { }
    return $null
}

function Get-LatestVersion {
    try {
        return (& npm view agent-browser version 2>$null | Select-Object -First 1).Trim()
    }
    catch { }
    return $null
}

$installed = Get-InstalledVersion
$latest = Get-LatestVersion

if (-not $installed) {
    Write-Host "agent-browser not found; installing latest ..."
    npm i -g agent-browser@latest
}
elseif ($latest -and ($installed -ne $latest)) {
    Write-Host "agent-browser update available: $installed -> $latest; updating ..."
    npm i -g agent-browser@latest
}
else {
    Write-Host "agent-browser $installed is current."
}

$npmPrefix = & npm prefix -g
if ($npmPrefix -and (-not ($env:Path -split ';' -contains $npmPrefix))) {
    $env:Path = "$npmPrefix;$env:Path"
}

if (-not (Get-InstalledVersion)) {
    throw "agent-browser still not available after install. Check npm global bin is on PATH."
}

Write-Host "Installing browser runtime ..."
& agent-browser install

Write-Host "agent-browser ready: $(& agent-browser --version)"
