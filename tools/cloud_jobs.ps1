param(
    [ValidateSet('Status', 'Run', 'Pause', 'Resume', 'Logs')]
    [string]$Action = 'Status',
    [ValidateSet('infra', 'production', 'currents')]
    [string]$Kind
)

$ErrorActionPreference = 'Stop'
if ($Action -ne 'Status' -and -not $Kind) { throw 'Supply -Kind infra, production, or currents.' }
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerExecutable = if ($dockerCommand) { $dockerCommand.Source } else {
    Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
}
if (-not (Test-Path -LiteralPath $dockerExecutable)) { throw 'Start Docker Desktop first.' }
$originalPath = $env:PATH
$env:PATH = "$(Split-Path -Parent $dockerExecutable);$originalPath"
$common = @('run', '--rm', '-v', 'bluewater-gcloud:/root/.config/gcloud',
    'gcr.io/google.com/cloudsdktool/google-cloud-cli:stable', 'gcloud')
$projectArgument = '--project=bluewater-512614'
$jobName = "bw-staging-refresh-$Kind"

function Invoke-JobCommand {
    param([string[]]$CommandArguments)
    & $dockerExecutable @common @CommandArguments
    if ($LASTEXITCODE -ne 0) { throw "Cloud jobs command failed (exit $LASTEXITCODE)." }
}

try {
    switch ($Action) {
        'Status' {
            Invoke-JobCommand @('scheduler', 'jobs', 'list', '--location=us-south1', $projectArgument,
                '--format=table(name,schedule,timeZone,state)')
            Invoke-JobCommand @('run', 'jobs', 'list', '--region=us-south1', $projectArgument)
        }
        'Run' {
            Invoke-JobCommand @('run', 'jobs', 'execute', $jobName, '--region=us-south1', $projectArgument)
        }
        'Pause' {
            Invoke-JobCommand @('scheduler', 'jobs', 'pause', $jobName, '--location=us-south1', $projectArgument)
        }
        'Resume' {
            Invoke-JobCommand @('scheduler', 'jobs', 'resume', $jobName, '--location=us-south1', $projectArgument)
        }
        'Logs' {
            Invoke-JobCommand @('run', 'jobs', 'logs', 'read', $jobName, '--region=us-south1',
                $projectArgument, '--limit=80')
        }
    }
} finally {
    $env:PATH = $originalPath
}
