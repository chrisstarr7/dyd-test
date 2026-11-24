# Bootstrap agent-browser for dyd-test. Idempotent; safe to rerun.
$ErrorActionPreference = "Stop"
$required = "0.44.0"

function Test-AgentBrowser {
    try {
        $v = & agent-browser --version 2>$null
        return ($v -match "agent-browser")
    }
    catch {
        return $false
    }
}

if (-not (Test-AgentBrowser)) {
    Write-Host "agent-browser not found; installing agent-browser@$required ..."
    npm i -g "agent-browser@$required"
}

$npmPrefix = & npm prefix -g
if ($npmPrefix -and (-not ($env:Path -split ';' -contains $npmPrefix))) {
    $env:Path = "$npmPrefix;$env:Path"
}

if (-not (Test-AgentBrowser)) {
    throw "agent-browser still not available after install. Check npm global bin is on PATH."
}

Write-Host "Installing browser runtime ..."
& agent-browser install

Write-Host "agent-browser ready: $(& agent-browser --version)"
