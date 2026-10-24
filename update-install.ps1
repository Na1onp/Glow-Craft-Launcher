param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$CurrentDirectory,
    [Parameter(Mandatory=$true)][string]$CandidateDirectory,
    [Parameter(Mandatory=$true)][string]$BackupDirectory
)
$ErrorActionPreference = 'Stop'
try {
    $process = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    if ($process) { $process.WaitForExit() }
    Start-Sleep -Milliseconds 800
    if (-not (Test-Path (Join-Path $CandidateDirectory 'Glow Craft Launcher.exe'))) {
        throw 'Staged launcher executable is missing.'
    }
    if (Test-Path $BackupDirectory) { Remove-Item -LiteralPath $BackupDirectory -Recurse -Force }
    Move-Item -LiteralPath $CurrentDirectory -Destination $BackupDirectory
    try {
        Move-Item -LiteralPath $CandidateDirectory -Destination $CurrentDirectory
    } catch {
        Move-Item -LiteralPath $BackupDirectory -Destination $CurrentDirectory
        throw
    }
    Start-Process -FilePath (Join-Path $CurrentDirectory 'Glow Craft Launcher.exe')
    Remove-Item -LiteralPath (Split-Path -Parent $CandidateDirectory) -Recurse -Force -ErrorAction SilentlyContinue
} catch {
    if (-not (Test-Path $CurrentDirectory) -and (Test-Path $BackupDirectory)) {
        Move-Item -LiteralPath $BackupDirectory -Destination $CurrentDirectory
    }
    if (Test-Path (Join-Path $CurrentDirectory 'Glow Craft Launcher.exe')) {
        Start-Process -FilePath (Join-Path $CurrentDirectory 'Glow Craft Launcher.exe')
    }
    exit 1
}
