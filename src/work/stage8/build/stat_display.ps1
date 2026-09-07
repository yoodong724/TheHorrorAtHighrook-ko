param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$PristinePath,
    [Parameter(Mandatory=$true)][string]$MapTsv,
    [Parameter(Mandatory=$true)][string]$CecilPath
)
$ErrorActionPreference = "Stop"
$runDir = Join-Path $env:TEMP ("highrook-stat-display-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $runDir | Out-Null
try {
    $inputLocal = Join-Path $runDir "input.dll"
    $outputLocal = Join-Path $runDir "output.dll"
    $pristineLocal = Join-Path $runDir "pristine.dll"
    $mapLocal = Join-Path $runDir "map.tsv"
    $cecilLocal = Join-Path $runDir "Mono.Cecil.dll"
    $sourceLocal = Join-Path $runDir "stat_display.cs"
    Copy-Item -LiteralPath $InputPath -Destination $inputLocal
    Copy-Item -LiteralPath $PristinePath -Destination $pristineLocal
    Copy-Item -LiteralPath $MapTsv -Destination $mapLocal
    Copy-Item -LiteralPath $CecilPath -Destination $cecilLocal
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "stat_display.cs") -Destination $sourceLocal
    [void][System.Reflection.Assembly]::LoadFrom($cecilLocal)
    Add-Type -Path $sourceLocal -ReferencedAssemblies $cecilLocal
    $result = [HighrookStatDisplayPatch]::Apply($inputLocal, $outputLocal, $pristineLocal, $mapLocal)
    Copy-Item -LiteralPath $outputLocal -Destination $OutputPath -Force
    Write-Output $result
}
finally {
    Remove-Item -LiteralPath $runDir -Recurse -Force -ErrorAction SilentlyContinue
}
