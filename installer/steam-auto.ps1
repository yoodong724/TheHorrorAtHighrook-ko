[CmdletBinding()]
param(
    [ValidateSet('Install','Restore')][string]$Action = 'Install',
    [switch]$DiscoverOnly,
    [switch]$Local,
    [string]$SteamRoot,
    [string]$Package
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if ([string]::IsNullOrWhiteSpace($Package)) { $Package = Join-Path $PSScriptRoot 'highrook-ko.patch.zip' }

$script:GameFolder = 'The Horror at Highrook'
$script:GameExe = 'TheHorrorAtHighrook.exe'
$script:PackageId = 'be4e30b724e1cc5c6e8192ff'

function Get-NormalPath([string]$Path) {
    return [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
}

function Add-UniquePath($Set, $List, [string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    try { $full = Get-NormalPath ([Environment]::ExpandEnvironmentVariables($Path.Trim().Trim('"'))) } catch { return }
    if ($Set.Add($full)) { [void]$List.Add($full) }
}

function Get-RegistrySteamRoots {
    $results = New-Object Collections.ArrayList
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $queries = @(
        [pscustomobject]@{ Key='HKCU:\Software\Valve\Steam'; Names=@('SteamPath','SteamExe') },
        [pscustomobject]@{ Key='HKLM:\Software\Valve\Steam'; Names=@('InstallPath') },
        [pscustomobject]@{ Key='HKLM:\Software\WOW6432Node\Valve\Steam'; Names=@('InstallPath') }
    )
    foreach ($query in $queries) {
        try { $row = Get-ItemProperty -LiteralPath $query.Key -ErrorAction Stop } catch { continue }
        foreach ($name in $query.Names) {
            $property = $row.PSObject.Properties[$name]
            if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) { continue }
            $value = [string]$property.Value
            if ($name -eq 'SteamExe') { $value = [IO.Path]::GetDirectoryName($value) }
            Add-UniquePath $seen $results $value
        }
    }
    $defaults = @()
    if (${env:ProgramFiles(x86)}) { $defaults += Join-Path ${env:ProgramFiles(x86)} 'Steam' }
    if ($env:ProgramFiles) { $defaults += Join-Path $env:ProgramFiles 'Steam' }
    if ($env:ProgramW6432) { $defaults += Join-Path $env:ProgramW6432 'Steam' }
    foreach ($candidate in $defaults) { Add-UniquePath $seen $results $candidate }
    return @($results)
}

function ConvertFrom-VdfQuoted([string]$Value) { return $Value.Replace('\\','\').Replace('\"','"') }

function Get-VdfLibraryRoots([string]$Root) {
    $results = New-Object Collections.ArrayList
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $vdf = Join-Path $Root 'steamapps\libraryfolders.vdf'
    if (-not (Test-Path -LiteralPath $vdf -PathType Leaf)) { return @() }
    $item = Get-Item -LiteralPath $vdf -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Steam libraryfolders.vdf is a reparse point: $vdf" }
    foreach ($line in [IO.File]::ReadLines($vdf, [Text.Encoding]::UTF8)) {
        $match = [Text.RegularExpressions.Regex]::Match($line, '^\s*"(?<key>path|[0-9]+)"\s*"(?<value>(?:\\.|[^"\\])*)"\s*$')
        if (-not $match.Success) { continue }
        $value = ConvertFrom-VdfQuoted $match.Groups['value'].Value
        if ($value -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)') { continue }
        Add-UniquePath $seen $results $value
    }
    return @($results)
}

function Find-GameCopies([string]$OverrideRoot) {
    $roots = New-Object Collections.ArrayList
    $rootSeen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    if (-not [string]::IsNullOrWhiteSpace($OverrideRoot)) {
        Add-UniquePath $rootSeen $roots $OverrideRoot
        $direct = Join-Path (Get-NormalPath $OverrideRoot) ('steamapps\common\' + $script:GameFolder)
        if (Test-Path -LiteralPath (Join-Path $direct $script:GameExe) -PathType Leaf) { return @((Get-NormalPath $direct)) }
        foreach ($library in @(Get-VdfLibraryRoots (Get-NormalPath $OverrideRoot))) { Add-UniquePath $rootSeen $roots $library }
    } else {
        foreach ($root in @(Get-RegistrySteamRoots)) {
            Add-UniquePath $rootSeen $roots $root
            foreach ($library in @(Get-VdfLibraryRoots $root)) { Add-UniquePath $rootSeen $roots $library }
        }
    }
    $copies = New-Object Collections.ArrayList
    $copySeen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($root in @($roots)) {
        $game = Join-Path $root ('steamapps\common\' + $script:GameFolder)
        $exe = Join-Path $game $script:GameExe
        if ((Test-Path -LiteralPath $game -PathType Container) -and (Test-Path -LiteralPath $exe -PathType Leaf)) { Add-UniquePath $copySeen $copies $game }
    }
    return @($copies)
}

function Select-SteamGame([string]$OverrideRoot) {
    $copies = @(Find-GameCopies $OverrideRoot)
    if ($copies.Count -eq 0) { throw 'The Horror at Highrook was not found in Steam libraries. Rerun with -SteamRoot pointing to the Steam/library root.' }
    if ($copies.Count -gt 1) { throw "Multiple game installations were found. Rerun with -SteamRoot pointing to the selected library root. Candidates: $($copies -join '; ')" }
    return $copies[0]
}

function Assert-NoReparseTree([string]$Root) {
    $full = Get-NormalPath $Root
    $cursor = Get-Item -LiteralPath $full -Force
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Steam target crosses a reparse point: $($cursor.FullName)" }
        $cursor = $cursor.Parent
    }
    foreach ($item in @(Get-ChildItem -LiteralPath $full -Force -Recurse)) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Steam target contains a reparse point: $($item.FullName)" }
    }
}

function Assert-GameNotRunning([string]$Target) {
    $expected = Get-NormalPath (Join-Path $Target $script:GameExe)
    foreach ($process in @(Get-Process -Name 'TheHorrorAtHighrook' -ErrorAction SilentlyContinue)) {
        try { $running = Get-NormalPath $process.MainModule.FileName }
        catch { throw "Cannot verify the executable path for running game PID $($process.Id): $($_.Exception.Message)" }
        if ($running.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) { throw "The Horror at Highrook is running from the selected Steam target. Exit the game before $Action." }
    }
}

function Get-DiscoveryResult([string]$OverrideRoot) {
    $copies = @(Find-GameCopies $OverrideRoot)
    return [ordered]@{status='discovered';readonly=$true;candidate_count=$copies.Count;candidates=$copies}
}

function Invoke-DirectInstallCore([string]$Target,[string]$PackagePath) { return Invoke-Install $Target $PackagePath }
function Invoke-DirectRestoreCore([string]$Target,[string]$PackagePath) { return Invoke-Restore $Target $script:PackageId $PackagePath }

function Invoke-SteamDirect([string]$RequestedAction,[string]$OverrideRoot,[string]$PackagePath) {
    if ($Local) {
        $target = Get-NormalPath (Join-Path $PSScriptRoot '..')
        if (-not (Test-Path -LiteralPath (Join-Path $target $script:GameExe) -PathType Leaf)) {
            throw 'Extract the highrook-ko folder into the game folder, then run highrook-ko\install.cmd.'
        }
    } else { $target = Select-SteamGame $OverrideRoot }
    Assert-NoReparseTree $target
    $installer = Join-Path $PSScriptRoot 'install.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw "Direct installer is missing: $installer" }
    . $installer -Target $target -Package $PackagePath
    Assert-GameNotRunning $target
    if ($RequestedAction -eq 'Install') {
        $inner = Invoke-DirectInstallCore $target $PackagePath
        return [ordered]@{status='installed_in_steam';target=$target;package_id=$inner.package_id;files=$inner.files;backup=$inner.backup}
    }
    $inner = Invoke-DirectRestoreCore $target $PackagePath
    return [ordered]@{status='restored_in_steam';target=$target;package_id=$inner.package_id;files=$inner.files;backup=$inner.backup}
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        if ($DiscoverOnly) { $result = Get-DiscoveryResult $SteamRoot }
        else { $result = Invoke-SteamDirect $Action $SteamRoot $Package }
        $result | ConvertTo-Json -Depth 6
    } catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}
