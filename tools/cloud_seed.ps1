param(
    [ValidateSet('infra', 'production', 'currents', 'contours', 'bathymetry', 'all')]
    [string]$Kind = 'all'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerExecutable = if ($dockerCommand) { $dockerCommand.Source } else {
    Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
}
if (-not (Test-Path -LiteralPath $dockerExecutable)) { throw 'Docker Desktop is required.' }
$originalPath = $env:PATH
$env:PATH = "$(Split-Path -Parent $dockerExecutable);$originalPath"
Push-Location $projectRoot
try {
    $kinds = if ($Kind -eq 'all') { @('infra', 'production', 'currents', 'contours', 'bathymetry') } else { @($Kind) }
    foreach ($dataset in $kinds) {
        Write-Host "Publishing $dataset to private Dallas staging storage..."
        $dockerArguments = @('compose', 'run', '--rm', '--user', '0:0',
            '-v', 'bluewater-gcloud:/credentials:ro',
            '-e', 'GOOGLE_APPLICATION_CREDENTIALS=/credentials/application_default_credentials.json',
            '-e', 'GOOGLE_CLOUD_PROJECT=bluewater-512614',
            '-e', 'BLUEWATER_STORAGE_BACKEND=gcs',
            '-e', 'BLUEWATER_DATA_BUCKET=bluewater-512614-bw-staging-data',
            '-e', 'BLUEWATER_BATHYMETRY_BUCKET=bluewater-512614-bw-staging-bathymetry',
            '-e', 'BLUEWATER_TILE_BUCKET=bluewater-512614-bw-staging-tiles',
            'worker', 'cloud')
        if ($dataset -eq 'bathymetry') { $dockerArguments += 'seed-bathymetry' }
        else { $dockerArguments += @('seed-data', '--kind', $dataset) }
        & $dockerExecutable @dockerArguments
        if ($LASTEXITCODE -ne 0) { throw "Publication of $dataset failed (exit $LASTEXITCODE)." }
    }
} finally {
    Pop-Location
    $env:PATH = $originalPath
}
