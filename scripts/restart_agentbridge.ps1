param(
    [Parameter(Mandatory = $true)][int]$OldPid,
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [int]$TimeoutSeconds = 60
)

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
    if ((Get-Date) -ge $deadline) { exit 2 }
    Start-Sleep -Milliseconds 250
}

Start-Process -FilePath $PythonExecutable -ArgumentList '-m', 'agentbridge.main' -WorkingDirectory $ProjectRoot -WindowStyle Hidden
