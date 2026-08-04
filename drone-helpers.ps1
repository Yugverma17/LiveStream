<#
  Shared functions for setting up one drone's detection service on the GPU server.
  Dot-sourced by both onboard-client.ps1 (loops this N times for a new client) and
  add-drone.ps1 (calls it once, for an existing client getting an extra drone).

  Design point: detection.py itself takes --stream/--name as command-line arguments
  (see the "MULTI-DRONE SUPPORT" block near the top of detection.py) rather than having
  the stream name hardcoded. That means ONE uploaded copy of detection.py on the GPU
  server serves every drone for that client - adding a drone is just a new systemd
  service pointing at the same file with a different --stream value, not a new file.

  Drone numbering convention (matches what's already deployed for existing clients):
    drone 1        -> stream "drone",   service "agrocast-detection"       (no suffix -
                      this is what already exists for every client onboarded before
                      multi-drone support, so it keeps working with zero changes)
    drone 2, 3, ... -> stream "droneN", service "agrocast-detection-droneN"
#>

function Get-DroneStreamName($DroneNum) {
    if ($DroneNum -eq 1) { "drone" } else { "drone$DroneNum" }
}

function Get-DroneServiceName($DroneNum) {
    if ($DroneNum -eq 1) { "agrocast-detection" } else { "agrocast-detection-drone$DroneNum" }
}

# Installs (or re-installs) the systemd service for one drone's detection.py process.
# Assumes detection.py has ALREADY been uploaded to /home/ubuntu/detection.py on the
# GPU server (onboard-client.ps1 does this once; add-drone.ps1 uploads it too if it's
# somehow missing, so it's self-sufficient against an older client).
function Install-DroneService {
    param(
        [Parameter(Mandatory=$true)][string]$GpuIP,
        [Parameter(Mandatory=$true)][string]$MainKey,
        [Parameter(Mandatory=$true)][int]$DroneNum,
        [string]$DroneName = $null
    )
    $streamName  = Get-DroneStreamName $DroneNum
    $serviceName = Get-DroneServiceName $DroneNum
    $nameArg = if ($DroneName) { "--name `"$DroneName`"" } else { "" }
    # drone 1 gets no --stream/--name args at all, so its ExecStart is byte-identical
    # to what's already deployed for every existing single-drone client.
    $execArgs = if ($DroneNum -eq 1) { "" } else { "--stream $streamName $nameArg" }

    $unit = @"
[Unit]
Description=Agrocast AI detection ($streamName)
After=network.target
[Service]
ExecStart=/opt/pytorch/bin/python /home/ubuntu/detection.py $execArgs
WorkingDirectory=/home/ubuntu
Restart=always
User=ubuntu
StandardOutput=append:/home/ubuntu/detection-$streamName.log
StandardError=append:/home/ubuntu/detection-$streamName.log
[Install]
WantedBy=multi-user.target
"@

    $remoteCmd = @"
set -e
if [ ! -f /home/ubuntu/detection.py ]; then
  echo 'detection.py missing on this server - upload it first' >&2
  exit 9
fi
cat > /tmp/$serviceName.service <<'EOF'
$unit
EOF
sudo mv /tmp/$serviceName.service /etc/systemd/system/$serviceName.service
sudo systemctl daemon-reload
sudo systemctl enable --now $serviceName
sleep 2
sudo systemctl is-active $serviceName
"@
    # NOT "$remoteCmd | ssh ... bash -s" - confirmed by a real failed run (2026-08-04) that
    # piping a multi-line string straight into a native process's stdin lets PowerShell 5.1
    # silently mangle it (a BOM appeared before the first line, and a stray \r got attached
    # to $serviceName, corrupting the systemd unit name - "agrocast-detection-drone2\r" -
    # into an invalid one). The string itself was verified byte-clean beforehand; the
    # corruption is specifically in that pipe-to-native-stdin path. Writing to a local file
    # with an explicit no-BOM encoding, then scp+ssh-execute (the same reliable pattern
    # already used everywhere else in these scripts for the actual code files), sidesteps
    # it entirely.
    $localScript = "$env:TEMP\agrocast-install-$serviceName.sh"
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($localScript, $remoteCmd, $utf8NoBom)
    scp -i $MainKey -o StrictHostKeyChecking=accept-new $localScript ubuntu@${GpuIP}:/tmp/install_$serviceName.sh | Out-Null
    ssh -i $MainKey -o StrictHostKeyChecking=accept-new ubuntu@$GpuIP "bash /tmp/install_$serviceName.sh && rm -f /tmp/install_$serviceName.sh"
    return $LASTEXITCODE -eq 0
}

# Adds the website-side camera entry for one drone, via the same /add_camera endpoint
# the "+ Add Camera" button uses.
function Add-DroneCamera {
    param(
        [Parameter(Mandatory=$true)][string]$StreamIP,
        [Parameter(Mandatory=$true)][string]$ApiKey,
        [Parameter(Mandatory=$true)][int]$DroneNum,
        [string]$DroneName = $null
    )
    $streamName = Get-DroneStreamName $DroneNum
    $camName = if ($DroneName) { $DroneName } else { "Camera $DroneNum" }
    $body = @{ name = $camName; stream = $streamName } | ConvertTo-Json -Compress

    # NOT "-d $body" - confirmed by a real failed run (2026-08-04) that PowerShell mangles
    # a JSON string containing embedded double-quotes when passed as a native command's
    # argument (Content-Length arrived as 29 bytes for a 38-byte string - silently
    # truncated). The camera was NEVER actually created, but curl's own exit code was
    # still 0 (curl doesn't treat an HTTP 400 as a failure), so the old check here
    # reported success anyway. Writing the JSON to a file and using curl's "@file" syntax
    # avoids the command-line argument entirely; checking the actual response body
    # (not just curl's exit code) is what catches a real API-level rejection.
    $bodyFile = "$env:TEMP\agrocast-addcam-$streamName.json"
    [System.IO.File]::WriteAllText($bodyFile, $body, (New-Object System.Text.UTF8Encoding $false))
    $resp = curl.exe -s -m 10 -X POST "http://${StreamIP}:5000/add_camera?apikey=$ApiKey" `
        -H "Content-Type: application/json" --data-binary "@$bodyFile"
    Remove-Item $bodyFile -ErrorAction SilentlyContinue
    return $resp -match '"status"\s*:\s*"ok"'
}

# Each drone is a SEPARATE aircraft with its own RC, so each needs its OWN gimbal script
# hosted at its own URL - gimbal_control.py hardcodes CAMERA_ID same as detection.py did,
# and unlike detection.py it runs via curl+python on the RC (not systemd with args we
# control), so the simplest correct fix is the same template-substitution approach
# already used for EC2_IP/API_KEY: bake the right CAMERA_ID into each drone's own copy.
#
# Drone 1 keeps the EXACT original path (clients/$Slug/gimbal_control.py) with no
# subfolder - this is the path already deployed and already fetched by every existing
# single-drone client's RC, so it must not move. Drones 2+ get their own subfolder.
function Publish-DroneGimbalScript {
    param(
        [Parameter(Mandatory=$true)][string]$WebKey,
        [Parameter(Mandatory=$true)][string]$WebServer,
        [Parameter(Mandatory=$true)][string]$Slug,
        [Parameter(Mandatory=$true)][int]$DroneNum,
        [Parameter(Mandatory=$true)][string]$BaseGimbalControlPath,  # already has EC2_IP/API_KEY substituted
        [Parameter(Mandatory=$true)][string]$Tmp
    )
    $streamName = Get-DroneStreamName $DroneNum
    $subpath = if ($DroneNum -eq 1) { "$Slug" } else { "$Slug/drone$DroneNum" }
    $remoteBase = "https://livestreamxk.com/clients/$subpath"

    $gimbalContent = (Get-Content $BaseGimbalControlPath -Raw) `
        -replace 'CAMERA_ID\s*=\s*"[^"]*"', "CAMERA_ID = `"cam_$streamName`""
    $startScript = @"
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null
cd ~ || exit 1
while true; do
  if curl -sf -o gimbal_control.py.new $remoteBase/gimbal_control.py; then
    mv gimbal_control.py.new gimbal_control.py
  fi
  python gimbal_control.py
  sleep 3
done
"@

    $tag = "d$DroneNum"
    Set-Content -Path "$Tmp\gc_$tag.py" -Value $gimbalContent -NoNewline
    Set-Content -Path "$Tmp\sg_$tag.sh" -Value $startScript -NoNewline

    ssh -i $WebKey -o StrictHostKeyChecking=accept-new ubuntu@$WebServer "sudo mkdir -p /var/www/liveStreamXK/clients/$subpath"
    scp -i $WebKey -o StrictHostKeyChecking=accept-new "$Tmp\gc_$tag.py" ubuntu@${WebServer}:~/gc_${Slug}_$tag.py
    $ok1 = $LASTEXITCODE -eq 0
    scp -i $WebKey -o StrictHostKeyChecking=accept-new "$Tmp\sg_$tag.sh" ubuntu@${WebServer}:~/sg_${Slug}_$tag.sh
    $ok2 = $LASTEXITCODE -eq 0
    ssh -i $WebKey -o StrictHostKeyChecking=accept-new ubuntu@$WebServer "sudo mv ~/gc_${Slug}_$tag.py /var/www/liveStreamXK/clients/$subpath/gimbal_control.py && sudo mv ~/sg_${Slug}_$tag.sh /var/www/liveStreamXK/clients/$subpath/start-gimbal.sh && sudo chown -R www-data:www-data /var/www/liveStreamXK/clients/$subpath"
    $ok3 = $LASTEXITCODE -eq 0
    return ($ok1 -and $ok2 -and $ok3)
}
