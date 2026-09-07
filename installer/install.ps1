[CmdletBinding()]
param(
    [ValidateSet('Install','Restore')][string]$Action = 'Install',
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$Package,
    [string]$PackageId = 'be4e30b724e1cc5c6e8192ff'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if ([string]::IsNullOrWhiteSpace($Package)) { $Package = Join-Path $PSScriptRoot 'highrook-ko-test.patch.zip' }

$script:ExpectedPackageSha256 = '0f2daafdeebf65eff98e06521ff8fdcf8e571bd829de4110b2db1f1640a3282c'
$script:ExpectedPackageId = 'be4e30b724e1cc5c6e8192ff'
$script:ExpectedFormat = 'highrook-byte-delta-v1'
$script:ExpectedBaselineCount = 1684
$script:ExpectedDeltaCount = 857
$script:BackupRootName = '.highrook-ko-backup'

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Get-NormalPath([string]$Path) {
    return [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
}

function Get-Sha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Test-LowerSha256($Value) {
    return ($Value -is [string] -and ([string]$Value) -cmatch '^[0-9a-f]{64}$')
}

function Assert-SafeRelativePath([string]$Relative, [string]$Label) {
    if ([string]::IsNullOrEmpty($Relative) -or $Relative.Contains('\') -or $Relative.Contains([char]0) -or $Relative.Contains(':')) {
        throw "$Label is unsafe: $Relative"
    }
    if ($Relative.StartsWith('/') -or $Relative.EndsWith('/')) { throw "$Label is unsafe: $Relative" }
    foreach ($part in $Relative.Split('/')) {
        if ([string]::IsNullOrEmpty($part) -or $part -eq '.' -or $part -eq '..') { throw "$Label is unsafe: $Relative" }
    }
    return $Relative
}

function Assert-NoReparsePath([string]$Path, [string]$Root, [bool]$AllowMissingLeaf, [string]$Label) {
    $rootFull = Get-NormalPath $Root
    $full = Get-NormalPath $Path
    if (-not $full.StartsWith($rootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escapes target: $full"
    }
    $relative = $full.Substring($rootFull.Length).TrimStart([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $current = $rootFull
    $parts = @($relative.Split([IO.Path]::DirectorySeparatorChar))
    for ($index = 0; $index -lt $parts.Count; $index++) {
        $current = Join-Path $current $parts[$index]
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label crosses a reparse point: $current" }
            if ($index -lt ($parts.Count - 1) -and -not $item.PSIsContainer) { throw "$Label has a non-directory parent: $current" }
        } elseif ($index -eq ($parts.Count - 1) -and -not $AllowMissingLeaf) {
            throw "$Label is missing: $full"
        }
    }
    return $full
}

function Resolve-TargetRoot([string]$Path) {
    $full = Get-NormalPath $Path
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw "Target must be an existing directory: $full" }
    $item = Get-Item -LiteralPath $full -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Target root is a reparse point: $full" }
    return $full
}

function Resolve-TargetFile([string]$Root, [string]$Relative, [bool]$AllowMissing) {
    [void](Assert-SafeRelativePath $Relative 'Target path')
    $path = Join-Path $Root $Relative.Replace('/', [IO.Path]::DirectorySeparatorChar)
    $full = Assert-NoReparsePath $path $Root $AllowMissing 'Target path'
    if (Test-Path -LiteralPath $full) {
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "Target path is not a file: $Relative" }
    } elseif (-not $AllowMissing) { throw "Required target file is missing: $Relative" }
    return $full
}

function Get-Properties($Object) { return @($Object.PSObject.Properties) }

function Assert-ExactProperties($Object, [string[]]$Expected, [string]$Label) {
    $actual = @(Get-Properties $Object | ForEach-Object { $_.Name } | Sort-Object)
    $wanted = @($Expected | Sort-Object)
    if (($actual -join "`n") -cne ($wanted -join "`n")) { throw "$Label has unexpected or missing fields." }
}

function Read-ZipText($Entry, [string]$Label) {
    $reader = New-Object IO.StreamReader($Entry.Open(), (New-Object Text.UTF8Encoding($false, $true)), $true)
    try { return $reader.ReadToEnd() }
    catch { throw "$Label is not valid UTF-8: $($_.Exception.Message)" }
    finally { $reader.Dispose() }
}

function Assert-ZipEntries($Entries) {
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $seenInsensitive = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in @($Entries)) {
        $name = [string]$entry.FullName
        [void](Assert-SafeRelativePath $name 'ZIP member')
        if (-not $seen.Add($name) -or -not $seenInsensitive.Add($name)) { throw "ZIP contains duplicate member: $name" }
        if ([string]::IsNullOrEmpty($entry.Name)) { throw "ZIP directory members are forbidden: $name" }
    }
}

function Assert-ManifestShape($Manifest, $Entries) {
    Assert-True ([string]$Manifest.format -ceq $script:ExpectedFormat) 'Unsupported package format.'
    Assert-True ([string]$Manifest.schema_version -ceq '1.0.0') 'Unsupported package schema version.'
    Assert-True ([string]$Manifest.package_id -ceq $script:ExpectedPackageId) 'Package ID does not match the pinned t05 bundle.'
    Assert-True ([string]$Manifest.metadata.mode -ceq 'test' -and [bool]$Manifest.metadata.release -eq $false) 'Package is not the pinned test-mode bundle.'

    $baseline = @($Manifest.metadata.full_baseline.files)
    Assert-True ([string]$Manifest.metadata.full_baseline.algorithm -ceq 'sha256') 'Full baseline algorithm must be sha256.'
    Assert-True ([int64]$Manifest.metadata.full_baseline.file_count -eq $script:ExpectedBaselineCount) 'Full baseline declared count is invalid.'
    Assert-True ($baseline.Count -eq $script:ExpectedBaselineCount) 'Full baseline row count is invalid.'
    Assert-True (@($Manifest.metadata.new_outputs).Count -eq 0) 'Pinned t05 bundle must not declare new outputs.'

    $baselinePaths = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($row in $baseline) {
        Assert-ExactProperties $row @('path','sha256') 'Full baseline row'
        $relative = Assert-SafeRelativePath ([string]$row.path) 'Full baseline path'
        Assert-True ($baselinePaths.Add($relative)) "Duplicate full baseline path: $relative"
        Assert-True (Test-LowerSha256 $row.sha256) "Invalid full baseline hash: $relative"
    }

    $files = @($Manifest.files)
    Assert-True ($files.Count -eq $script:ExpectedDeltaCount) 'Delta file count is invalid.'
    $filePaths = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $payloadNames = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $expectedMembers = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    [void]$expectedMembers.Add('manifest.json')
    foreach ($row in $files) {
        Assert-ExactProperties $row @('path','input_sha256','input_size','input_mode','output_sha256','output_size','mode','payload','payload_sha256','payload_size','ops') 'Delta row'
        $relative = Assert-SafeRelativePath ([string]$row.path) 'Delta path'
        $payload = Assert-SafeRelativePath ([string]$row.payload) 'Payload path'
        Assert-True ($filePaths.Add($relative)) "Duplicate delta path: $relative"
        Assert-True ($payloadNames.Add($payload)) "Duplicate payload path: $payload"
        Assert-True ($expectedMembers.Add($payload)) "Duplicate expected ZIP member: $payload"
        Assert-True ($baselinePaths.Contains($relative)) "Delta input is absent from full baseline: $relative"
        Assert-True (Test-LowerSha256 $row.input_sha256) "Invalid input hash: $relative"
        Assert-True (Test-LowerSha256 $row.output_sha256) "Invalid output hash: $relative"
        Assert-True (Test-LowerSha256 $row.payload_sha256) "Invalid payload hash: $relative"
        Assert-True ([int64]$row.input_size -ge 0 -and [int64]$row.output_size -ge 0 -and [int64]$row.payload_size -ge 0) "Invalid size: $relative"
        Assert-True (@($row.ops).Count -gt 0) "Delta operation list is empty: $relative"
        [int64]$totalOutput = 0
        foreach ($operation in @($row.ops)) {
            $properties = @(Get-Properties $operation)
            Assert-True ($properties.Count -eq 1 -and ($properties[0].Name -ceq 'copy' -or $properties[0].Name -ceq 'insert')) "Invalid delta operation: $relative"
            $range = @($properties[0].Value)
            Assert-True ($range.Count -eq 2) "Invalid delta range: $relative"
            $offset = [int64]$range[0]; $length = [int64]$range[1]
            $limit = if ($properties[0].Name -ceq 'copy') { [int64]$row.input_size } else { [int64]$row.payload_size }
            Assert-True ($offset -ge 0 -and $length -ge 0 -and $offset -le $limit -and $length -le ($limit - $offset)) "Delta range exceeds source: $relative"
            Assert-True ($length -le ([int64]$row.output_size - $totalOutput)) "Delta operations exceed output size: $relative"
            $totalOutput += $length
        }
        Assert-True ($totalOutput -eq [int64]$row.output_size) "Delta operations do not produce declared output size: $relative"
    }
    $actualMembers = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in @($Entries)) { [void]$actualMembers.Add([string]$entry.FullName) }
    Assert-True ($actualMembers.SetEquals($expectedMembers)) 'ZIP contains undeclared or missing members.'
}

function Open-ValidatedPackage([string]$PackagePath) {
    $full = Get-NormalPath $PackagePath
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "Package is missing: $full" }
    $item = Get-Item -LiteralPath $full -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Package is a reparse point: $full" }
    Assert-True ((Get-Sha256 $full) -ceq $script:ExpectedPackageSha256) 'Package SHA-256 does not match the pinned t05 bundle.'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($full)
    try {
        $entries = @($zip.Entries)
        Assert-ZipEntries $entries
        $manifestEntries = @($entries | Where-Object { $_.FullName -ceq 'manifest.json' })
        Assert-True ($manifestEntries.Count -eq 1) 'ZIP must contain exactly one root manifest.json.'
        try { $manifest = (Read-ZipText $manifestEntries[0] 'Package manifest') | ConvertFrom-Json }
        catch { throw "Package manifest is invalid JSON: $($_.Exception.Message)" }
        Assert-ManifestShape $manifest $entries
        return [pscustomobject]@{ Path=$full; Zip=$zip; Manifest=$manifest; Entries=$entries }
    } catch { $zip.Dispose(); throw }
}

function Copy-Range([IO.Stream]$Source, [IO.Stream]$Destination, [int64]$Offset, [int64]$Length) {
    [void]$Source.Seek($Offset, [IO.SeekOrigin]::Begin)
    $buffer = New-Object byte[] 1048576
    $remaining = $Length
    while ($remaining -gt 0) {
        $wanted = [int][Math]::Min([int64]$buffer.Length, $remaining)
        $read = $Source.Read($buffer, 0, $wanted)
        if ($read -le 0) { throw 'Delta source ended before its declared range.' }
        $Destination.Write($buffer, 0, $read)
        $remaining -= $read
    }
}

function Copy-PayloadAndVerify($Entry, $Row, [string]$Path) {
    $input = $Entry.Open(); $output = [IO.File]::Create($Path)
    try { $input.CopyTo($output) } finally { $output.Dispose(); $input.Dispose() }
    Assert-True ((Get-Item -LiteralPath $Path).Length -eq [int64]$Row.payload_size) "Payload size mismatch: $($Row.path)"
    Assert-True ((Get-Sha256 $Path) -ceq [string]$Row.payload_sha256) "Payload hash mismatch: $($Row.path)"
}

function Build-StagedOutputs([string]$TargetRoot, $PackageInfo, [string]$StageRoot) {
    $entryMap = @{}
    foreach ($entry in $PackageInfo.Entries) { $entryMap[[string]$entry.FullName] = $entry }
    $number = 0
    foreach ($row in @($PackageInfo.Manifest.files)) {
        $relative = [string]$row.path
        $sourcePath = Resolve-TargetFile $TargetRoot $relative $false
        $payloadPath = Join-Path $StageRoot ('payload-{0:D4}.bin' -f $number)
        Copy-PayloadAndVerify $entryMap[[string]$row.payload] $row $payloadPath
        $outputPath = Join-Path $StageRoot ('output-{0:D4}.bin' -f $number)
        $base = [IO.File]::OpenRead($sourcePath); $payload = [IO.File]::OpenRead($payloadPath); $output = [IO.File]::Create($outputPath)
        try {
            foreach ($operation in @($row.ops)) {
                $property = @(Get-Properties $operation)[0]
                $range = @($property.Value)
                $stream = if ($property.Name -ceq 'copy') { $base } else { $payload }
                Copy-Range $stream $output ([int64]$range[0]) ([int64]$range[1])
            }
        } finally { $output.Dispose(); $payload.Dispose(); $base.Dispose() }
        Assert-True ((Get-Item -LiteralPath $outputPath).Length -eq [int64]$row.output_size) "Rebuilt output size mismatch: $relative"
        Assert-True ((Get-Sha256 $outputPath) -ceq [string]$row.output_sha256) "Rebuilt output hash mismatch: $relative"
        Remove-Item -LiteralPath $payloadPath -Force
        $number++
    }
}

function Assert-Baseline([string]$TargetRoot, $Manifest) {
    foreach ($row in @($Manifest.metadata.full_baseline.files)) {
        $path = Resolve-TargetFile $TargetRoot ([string]$row.path) $false
        Assert-True ((Get-Sha256 $path) -ceq [string]$row.sha256) "Wrong game version or modified baseline file: $($row.path)"
    }
    foreach ($row in @($Manifest.files)) {
        $path = Resolve-TargetFile $TargetRoot ([string]$row.path) $false
        Assert-True ((Get-Item -LiteralPath $path).Length -eq [int64]$row.input_size) "Delta input size mismatch: $($row.path)"
        Assert-True ((Get-Sha256 $path) -ceq [string]$row.input_sha256) "Delta input hash mismatch: $($row.path)"
    }
}

function Write-Backup([string]$TargetRoot, $Manifest, [string]$Temporary, [string]$Final) {
    $filesRoot = Join-Path $Temporary 'files'
    [IO.Directory]::CreateDirectory($filesRoot) | Out-Null
    $rows = @()
    foreach ($row in @($Manifest.files)) {
        $relative = [string]$row.path
        $source = Resolve-TargetFile $TargetRoot $relative $false
        $saved = Join-Path $filesRoot $relative.Replace('/', [IO.Path]::DirectorySeparatorChar)
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($saved)) | Out-Null
        [IO.File]::Copy($source, $saved, $false)
        Assert-True ((Get-Sha256 $saved) -ceq [string]$row.input_sha256) "Backup hash mismatch while creating backup: $relative"
        $rows += [ordered]@{ path=$relative; input_sha256=[string]$row.input_sha256; output_sha256=[string]$row.output_sha256; input_mode=[int]$row.input_mode; mode=[int]$row.mode }
    }
    $backupManifest = [ordered]@{ schema_version='1.0.0'; package_id=$script:ExpectedPackageId; files=$rows }
    [IO.File]::WriteAllText((Join-Path $Temporary 'backup.json'), ($backupManifest | ConvertTo-Json -Depth 8) + "`n", (New-Object Text.UTF8Encoding($false)))
    [IO.Directory]::Move($Temporary, $Final)
}

function Replace-FromFile([string]$Source, [string]$Destination) {
    $temporary = $Destination + '.highrook-tmp'
    if (Test-Path -LiteralPath $temporary) { throw "Replacement temporary file already exists: $temporary" }
    [IO.File]::Copy($Source, $temporary, $false)
    try {
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Invoke-Replacements([string]$TargetRoot, $Rows, [string]$SourceRoot, [string]$RollbackRoot, [ValidateSet('Install','Restore')][string]$Mode, [int]$FailureAfter = 0) {
    $script:ReplacementRollbackFailed = $false
    $changed = New-Object Collections.ArrayList
    try {
        for ($number = 0; $number -lt @($Rows).Count; $number++) {
            $row = @($Rows)[$number]; $relative = [string]$row.path
            $destination = Resolve-TargetFile $TargetRoot $relative $false
            $source = if ($Mode -eq 'Install') { Join-Path $SourceRoot ('output-{0:D4}.bin' -f $number) } else { Join-Path $SourceRoot $relative.Replace('/', [IO.Path]::DirectorySeparatorChar) }
            [void]$changed.Add($row)
            Replace-FromFile $source $destination
            $hashProperty = if ($Mode -eq 'Install') { 'output_sha256' } else { 'input_sha256' }
            if ($null -ne $row.PSObject.Properties[$hashProperty]) {
                Assert-True ((Get-Sha256 $destination) -ceq [string]$row.$hashProperty) "Replacement hash verification failed: $relative"
            }
            if ($FailureAfter -gt 0 -and $changed.Count -ge $FailureAfter) { throw "Injected partial-$($Mode.ToLowerInvariant()) failure." }
        }
    } catch {
        $failure = $_; $rollbackErrors = @()
        for ($index = $changed.Count - 1; $index -ge 0; $index--) {
            $row = $changed[$index]; $relative = [string]$row.path
            try {
                $destination = Resolve-TargetFile $TargetRoot $relative $false
                $rollback = Join-Path $RollbackRoot $relative.Replace('/', [IO.Path]::DirectorySeparatorChar)
                Replace-FromFile $rollback $destination
            } catch { $rollbackErrors += "$relative`: $($_.Exception.Message)" }
        }
        if ($rollbackErrors.Count -gt 0) {
            $script:ReplacementRollbackFailed = $true
            throw "Replacement failed ($($failure.Exception.Message)); rollback failed: $($rollbackErrors -join '; ')"
        }
        throw $failure
    }
}

function Read-Backup([string]$TargetRoot, [string]$Id) {
    Assert-True ($Id -ceq $script:ExpectedPackageId) 'Restore PackageId does not match the pinned t05 bundle.'
    $root = Join-Path (Join-Path $TargetRoot $script:BackupRootName) $Id
    $root = Assert-NoReparsePath $root $TargetRoot $false 'Backup directory'
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "Backup is missing: $Id" }
    $manifestPath = Assert-NoReparsePath (Join-Path $root 'backup.json') $TargetRoot $false 'Backup manifest'
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Backup manifest is invalid JSON: $($_.Exception.Message)" }
    Assert-True ([string]$manifest.schema_version -ceq '1.0.0' -and [string]$manifest.package_id -ceq $Id) 'Backup manifest identity is invalid.'
    Assert-True (@($manifest.files).Count -eq $script:ExpectedDeltaCount) 'Backup file count is invalid.'
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($row in @($manifest.files)) {
        Assert-ExactProperties $row @('path','input_sha256','output_sha256','input_mode','mode') 'Backup row'
        $relative = Assert-SafeRelativePath ([string]$row.path) 'Backup path'
        Assert-True ($seen.Add($relative)) "Duplicate backup path: $relative"
        Assert-True (Test-LowerSha256 $row.input_sha256) "Invalid backup input hash: $relative"
        Assert-True (Test-LowerSha256 $row.output_sha256) "Invalid backup output hash: $relative"
        $installed = Resolve-TargetFile $TargetRoot $relative $false
        Assert-True ((Get-Sha256 $installed) -ceq [string]$row.output_sha256) "Installed file changed; refusing restore: $relative"
        $saved = Assert-NoReparsePath (Join-Path (Join-Path $root 'files') $relative.Replace('/', [IO.Path]::DirectorySeparatorChar)) $TargetRoot $false 'Backup file'
        Assert-True ((Get-Sha256 $saved) -ceq [string]$row.input_sha256) "Backup hash mismatch: $relative"
    }
    return [pscustomobject]@{ Root=$root; Manifest=$manifest }
}

function Invoke-Install([string]$TargetPath, [string]$PackagePath) {
    $targetRoot = Resolve-TargetRoot $TargetPath
    $packageInfo = Open-ValidatedPackage $PackagePath
    $stage = Join-Path $targetRoot ('.highrook-ko-stage-' + [Guid]::NewGuid().ToString('N'))
    $backupParent = Join-Path $targetRoot $script:BackupRootName
    $backup = Join-Path $backupParent $script:ExpectedPackageId
    $backupTemp = Join-Path $targetRoot ('.highrook-ko-backup-' + [Guid]::NewGuid().ToString('N'))
    try {
        if (Test-Path -LiteralPath $backup) { throw "Backup already exists: $script:ExpectedPackageId" }
        if (Test-Path -LiteralPath $backupParent) { [void](Assert-NoReparsePath $backupParent $targetRoot $false 'Backup root') }
        Assert-Baseline $targetRoot $packageInfo.Manifest
        [IO.Directory]::CreateDirectory($stage) | Out-Null
        Build-StagedOutputs $targetRoot $packageInfo $stage
        [IO.Directory]::CreateDirectory($backupTemp) | Out-Null
        if (-not (Test-Path -LiteralPath $backupParent)) { [IO.Directory]::CreateDirectory($backupParent) | Out-Null }
        Write-Backup $targetRoot $packageInfo.Manifest $backupTemp $backup
        Invoke-Replacements $targetRoot @($packageInfo.Manifest.files) $stage (Join-Path $backup 'files') 'Install'
        return [ordered]@{ status='installed'; package_id=$script:ExpectedPackageId; files=$script:ExpectedDeltaCount; backup=$backup }
    } finally {
        $packageInfo.Zip.Dispose()
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
        if (Test-Path -LiteralPath $backupTemp) { Remove-Item -LiteralPath $backupTemp -Recurse -Force }
    }
}

function Invoke-Restore([string]$TargetPath, [string]$Id, [string]$PackagePath) {
    $targetRoot = Resolve-TargetRoot $TargetPath
    $packageInfo = Open-ValidatedPackage $PackagePath
    $rollback = Join-Path $targetRoot ('.highrook-ko-restore-rollback-' + [Guid]::NewGuid().ToString('N'))
    $preserveRollback = $false
    $script:ReplacementRollbackFailed = $false
    try {
        $backup = Read-Backup $targetRoot $Id
        for ($number = 0; $number -lt $script:ExpectedDeltaCount; $number++) {
            $backupRow = @($backup.Manifest.files)[$number]
            $packageRow = @($packageInfo.Manifest.files)[$number]
            Assert-True ([string]$backupRow.path -ceq [string]$packageRow.path) "Backup/package path mismatch at row $number."
            Assert-True ([string]$backupRow.input_sha256 -ceq [string]$packageRow.input_sha256) "Backup/package input hash mismatch: $($packageRow.path)"
            Assert-True ([string]$backupRow.output_sha256 -ceq [string]$packageRow.output_sha256) "Backup/package output hash mismatch: $($packageRow.path)"
            Assert-True ([int]$backupRow.input_mode -eq [int]$packageRow.input_mode -and [int]$backupRow.mode -eq [int]$packageRow.mode) "Backup/package mode mismatch: $($packageRow.path)"
        }
        [IO.Directory]::CreateDirectory($rollback) | Out-Null
        foreach ($row in @($backup.Manifest.files)) {
            $relative = [string]$row.path
            $source = Resolve-TargetFile $targetRoot $relative $false
            $saved = Join-Path $rollback $relative.Replace('/', [IO.Path]::DirectorySeparatorChar)
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($saved)) | Out-Null
            [IO.File]::Copy($source, $saved, $false)
        }
        Invoke-Replacements $targetRoot @($backup.Manifest.files) (Join-Path $backup.Root 'files') $rollback 'Restore'
        return [ordered]@{ status='restored'; package_id=$Id; files=$script:ExpectedDeltaCount; backup=$backup.Root }
    } catch {
        if ($script:ReplacementRollbackFailed) { $preserveRollback = $true }
        throw
    } finally {
        $packageInfo.Zip.Dispose()
        if (-not $preserveRollback -and (Test-Path -LiteralPath $rollback)) { Remove-Item -LiteralPath $rollback -Recurse -Force }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        if ($Action -eq 'Install') {
            Assert-True ($PackageId -ceq $script:ExpectedPackageId) 'Install PackageId does not match the pinned t05 bundle.'
            $result = Invoke-Install $Target $Package
        } else { $result = Invoke-Restore $Target $PackageId $Package }
        $result | ConvertTo-Json -Depth 6
    } catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}

