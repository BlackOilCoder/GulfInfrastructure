param([ValidateRange(1024, 65535)][int]$Port = 8082)
$ErrorActionPreference = 'Stop'
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerExecutable = if ($dockerCommand) { $dockerCommand.Source } else {
    Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
}
if (-not (Test-Path -LiteralPath $dockerExecutable)) { throw 'Start Docker Desktop first.' }
Write-Host "Private staging will be available at http://127.0.0.1:$Port/ . Leave this terminal open; Ctrl+C stops the proxy."
& $dockerExecutable run --rm -it -p "127.0.0.1:${Port}:8080" `
    -v bluewater-gcloud:/root/.config/gcloud `
    gcr.io/google.com/cloudsdktool/google-cloud-cli:stable `
    gcloud run services proxy bw-staging-web --project=bluewater-512614 `
    --region=us-south1 --host=0.0.0.0 --port=8080
if ($LASTEXITCODE -ne 0) { throw 'Staging proxy failed. Check Docker and cloud_infra.ps1 Login.' }
