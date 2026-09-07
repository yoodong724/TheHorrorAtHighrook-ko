param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$PatchJson,
    [Parameter(Mandatory=$true)][string]$CecilPath
)
$ErrorActionPreference = "Stop"
$runDir = Join-Path $env:TEMP ("highrook-extra-il-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $runDir | Out-Null
try {
    $inputLocal = Join-Path $runDir "input.dll"
    $outputLocal = Join-Path $runDir "output.dll"
    $jsonLocal = Join-Path $runDir "patches.json"
    $tsvLocal = Join-Path $runDir "patches.tsv"
    $cecilLocal = Join-Path $runDir "Mono.Cecil.dll"
    $sourceLocal = Join-Path $runDir "patch_il.cs"
    Copy-Item -LiteralPath $InputPath -Destination $inputLocal
    Copy-Item -LiteralPath $PatchJson -Destination $jsonLocal
    Copy-Item -LiteralPath $CecilPath -Destination $cecilLocal
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "patch_il.cs") -Destination $sourceLocal
    # Windows PowerShell 5.1 treats BOM-less text as the active ANSI code page
    # when -Encoding is omitted.  The Python caller deliberately writes UTF-8
    # JSON, so make that transport encoding explicit before producing the TSV.
    $patches = Get-Content -Raw -LiteralPath $jsonLocal -Encoding UTF8 | ConvertFrom-Json
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $lines = foreach ($patch in $patches) {
        $fields = @(
            $patch.id, $patch.type, $patch.method, $patch.method_token,
            [string]$patch.instruction_occurrence, [string]$patch.instruction_offset,
            $patch.source, $patch.source_sha256, $patch.ko
        )
        (($fields | ForEach-Object { [Convert]::ToBase64String($utf8.GetBytes([string]$_)) }) -join "`t")
    }
    [System.IO.File]::WriteAllLines($tsvLocal, $lines, $utf8)
    Copy-Item -LiteralPath $tsvLocal -Destination ([System.IO.Path]::ChangeExtension($PatchJson, ".tsv")) -Force
    [void][System.Reflection.Assembly]::LoadFrom($cecilLocal)
    Add-Type -Path $sourceLocal -ReferencedAssemblies $cecilLocal
    $result = [HighrookExtraIlPatch]::Apply($inputLocal, $outputLocal, $tsvLocal)
    Copy-Item -LiteralPath $outputLocal -Destination $OutputPath -Force
    Write-Output $result
}
finally {
    Remove-Item -LiteralPath $runDir -Recurse -Force -ErrorAction SilentlyContinue
}
