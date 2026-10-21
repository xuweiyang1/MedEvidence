# 在任何解压位置运行：powershell -ExecutionPolicy Bypass -File .\INSTALL_AGENT.ps1 -ProjectRoot "E:\Download\Agent\MedEvidence"
param([string]$ProjectRoot = (Get-Location).Path)
$ErrorActionPreference = 'Stop'
$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = (Resolve-Path -LiteralPath $ProjectRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $Target 'scripts'))) {
    throw "项目根目录未包含 scripts: $Target"
}
$backup = Join-Path $Target ('outputs\agent_backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$names = @('agent','scripts','tests','requirements-agent-sdk.txt','requirements-agent.txt',
           '.env.agent.example','README_AGENT.md','my_retrieval_bridge.example.py')
$copies = 0
foreach ($name in $names) {
    $from = Join-Path $Source $name
    if (-not (Test-Path -LiteralPath $from)) { continue }
    $item = Get-Item -LiteralPath $from
    $files = if ($item.PSIsContainer) { Get-ChildItem -LiteralPath $from -File -Recurse } else { @($item) }
    foreach ($file in $files) {
        $rel = $file.FullName.Substring($Source.Length).TrimStart('\','/')
        $dest = Join-Path $Target $rel
        if ([string]::Equals($file.FullName, $dest, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        if (Test-Path -LiteralPath $dest) {
            $safe = Join-Path $backup $rel
            New-Item -ItemType Directory -Path (Split-Path -Parent $safe) -Force | Out-Null
            Copy-Item -LiteralPath $dest -Destination $safe -Force
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $dest -Force
        $copies += 1
    }
}
$envExample = Join-Path $Target '.env.agent.example'
$envLive = Join-Path $Target '.env.agent'
if ((Test-Path -LiteralPath $envExample) -and -not (Test-Path -LiteralPath $envLive)) {
    Copy-Item -LiteralPath $envExample -Destination $envLive
}
Write-Host "Agent SDK file installation complete. Files copied: $copies" -ForegroundColor Green
Write-Host "Project root: $Target"
Write-Host "Existing files backed up under: $backup (only if overwritten)"
Write-Host "Config: $envLive (never overwritten if already exists)"
Write-Host "NEXT: python scripts\run_agent.py --probe"
