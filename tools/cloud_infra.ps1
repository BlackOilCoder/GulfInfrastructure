param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Login', 'Status', 'Init', 'Plan', 'Apply', 'Outputs')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$infraFolder = Join-Path $projectRoot 'deploy/terraform'
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerExecutable = if ($dockerCommand) { $dockerCommand.Source } else {
    Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
}
if (-not (Test-Path -LiteralPath $dockerExecutable)) {
    throw 'Docker Desktop is required. Start Docker Desktop, then run this command again.'
}
$originalPath = $env:PATH
$env:PATH = "$(Split-Path -Parent $dockerExecutable);$originalPath"
$cliImage = 'gcr.io/google.com/cloudsdktool/google-cloud-cli:stable'
$terraformImage = 'hashicorp/terraform:1.13.5'

function Invoke-CloudDocker {
    param([string[]]$DockerArguments)
    & $dockerExecutable @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw "Cloud deployment command failed (exit $LASTEXITCODE)." }
}

try {
    if ($Action -eq 'Login') {
        Invoke-CloudDocker -DockerArguments @('run', '--rm', '-it', '-v', 'bluewater-gcloud:/root/.config/gcloud',
            $cliImage, 'gcloud', 'auth', 'login', '--update-adc', '--no-launch-browser', '--project=bluewater-512614')
        return
    }
    if ($Action -eq 'Status') {
        $common = @('run', '--rm', '-v', 'bluewater-gcloud:/root/.config/gcloud', $cliImage, 'gcloud')
        Invoke-CloudDocker ($common + @('auth', 'list', '--format=table(account,status)'))
        Invoke-CloudDocker ($common + @('projects', 'describe', 'bluewater-512614', '--format=table(projectId,lifecycleState)'))
        Invoke-CloudDocker ($common + @('billing', 'projects', 'describe', 'bluewater-512614', '--format=value(billingEnabled)'))
        return
    }
    $common = @('run', '--rm', '--mount', "type=bind,source=$infraFolder,target=/infra",
        '-v', 'bluewater-gcloud:/credentials:ro',
        '-e', 'GOOGLE_APPLICATION_CREDENTIALS=/credentials/application_default_credentials.json',
        '-w', '/infra', $terraformImage)
    switch ($Action) {
        'Init'    { Invoke-CloudDocker ($common + @('init', '-input=false')) }
        'Plan'    { Invoke-CloudDocker ($common + @('plan', '-input=false', '-out=staging.tfplan')) }
        'Apply'   {
            if (-not (Test-Path -LiteralPath (Join-Path $infraFolder 'staging.tfplan'))) {
                throw 'Run Plan first. Apply only executes the reviewed saved staging plan.'
            }
            Invoke-CloudDocker ($common + @('apply', '-input=false', 'staging.tfplan'))
        }
        'Outputs' { Invoke-CloudDocker ($common + @('output', '-json')) }
    }
} finally {
    $env:PATH = $originalPath
}
