# Run interactively. Secret values are sent on stdin, never as command arguments,
# environment variables, Terraform values, or temporary files.
$ErrorActionPreference = 'Stop'
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerExecutable = if ($dockerCommand) { $dockerCommand.Source } else {
    Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
}
if (-not (Test-Path -LiteralPath $dockerExecutable)) { throw 'Start Docker Desktop first.' }

$username = Read-Host 'Copernicus Marine username'
if ([string]::IsNullOrWhiteSpace($username)) { throw 'Username cannot be empty.' }
$securePassword = Read-Host 'Copernicus Marine password' -AsSecureString
if ($securePassword.Length -eq 0) { throw 'Password cannot be empty.' }
$passwordPointer = [IntPtr]::Zero
$process = $null
try {
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    $payload = @{username=$username.Trim(); password=$plainPassword} | ConvertTo-Json -Compress
    # Use redirected UTF-8 stdin so Windows PowerShell 5.1 also preserves non-ASCII
    # characters. Docker is run without a TTY, so its input cannot be echoed.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $dockerExecutable
    $startInfo.Arguments = 'run --rm -i -v bluewater-gcloud:/root/.config/gcloud gcr.io/google.com/cloudsdktool/google-cloud-cli:stable gcloud secrets versions add bluewater-staging-copernicus --project=bluewater-512614 --data-file=- --quiet'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.EnvironmentVariables['PATH'] = "$(Split-Path -Parent $dockerExecutable);$env:PATH"
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    $process.StandardInput.BaseStream.Write($payloadBytes, 0, $payloadBytes.Length)
    $process.StandardInput.BaseStream.Flush()
    $process.StandardInput.Close()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Secret upload failed (exit $($process.ExitCode))." }
    Write-Host 'Copernicus credentials saved in Google Secret Manager.'
} finally {
    if ($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
    $plainPassword = $null
    $payload = $null
    if ($payloadBytes) { [Array]::Clear($payloadBytes, 0, $payloadBytes.Length) }
    $securePassword.Dispose()
    if ($process) { $process.Dispose() }
}
