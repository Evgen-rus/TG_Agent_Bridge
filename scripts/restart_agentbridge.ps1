param(
    [Parameter(Mandatory = $true)][int]$OldPid,
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [int]$TimeoutSeconds = 60
)

$logDirectory = Join-Path $ProjectRoot 'runtime\logs'
$logPath = Join-Path $logDirectory 'restart.log'
$stdoutPath = Join-Path $logDirectory 'restart-child.stdout.log'
$stderrPath = Join-Path $logDirectory 'restart-child.stderr.log'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-RestartLog([string]$Message) {
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $Message" -Encoding utf8
}

try {
    Write-RestartLog "helper_started old_pid=$OldPid"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $deadline) {
            Write-RestartLog "old_process_timeout old_pid=$OldPid"
            exit 2
        }
        Start-Sleep -Milliseconds 250
    }

    Write-RestartLog "old_process_exited old_pid=$OldPid"
    $child = Start-Process -FilePath $PythonExecutable -ArgumentList '-m', 'agentbridge.main' `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    Start-Sleep -Seconds 2
    if ($child.HasExited) {
        Write-RestartLog "child_exited pid=$($child.Id) exit_code=$($child.ExitCode)"
        exit 3
    }
    Write-RestartLog "child_started pid=$($child.Id)"
} catch {
    Write-RestartLog "helper_failed error=$($_.Exception.Message)"
    exit 1
}
