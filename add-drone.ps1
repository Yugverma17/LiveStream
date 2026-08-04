<#
  Adds ONE more drone to a client that's ALREADY been onboarded (via onboard-client.ps1),
  without redoing anything that already works. Use this instead of re-running
  onboard-client.ps1 when a client buys their 2nd/3rd/etc. drone later.

  Auto-detects the next drone number by asking the client's own API how many cameras it
  already has - you don't have to track or pass a drone number yourself.

  Usage:
    .\add-drone.ps1 -ClientName acmefarms -StreamIP <their streaming IP> -GpuIP <their GPU IP> -ApiKey <their existing key>

  What this does:
    1. Asks the client's API how many cameras already exist -> that decides this new
       drone's number (e.g. 3 existing cameras -> this becomes drone 4).
    2. Makes sure detection.py is on the GPU server (uploads our current copy if it's
       somehow missing - e.g. an older client from before multi-drone support existed).
    3. Adds a new systemd service for this drone's detection process.
    4. Hosts this drone's own gimbal script at its own URL (separate RC, separate
       CAMERA_ID - each drone is its own aircraft).
    5. Adds the camera on the website.
    6. Prints this drone's OBS Stream Key + Termux setup commands.

  IMPORTANT - not yet proven on real hardware for a genuinely NEW drone number (2+).
  Watch it closely the first time, same as onboard-client.ps1 was.
#>

param(
    [Parameter(Mandatory=$true)][string]$ClientName,
    [Parameter(Mandatory=$true)][string]$StreamIP,
    [Parameter(Mandatory=$true)][string]$GpuIP,
    [Parameter(Mandatory=$true)][string]$ApiKey,
    [string]$DroneName = $null
)

$ErrorActionPreference = "Continue"
$Dir       = "D:\Live Stream"
$MainKey   = "$Dir\agrocast-main-key.pem"
$WebKey    = "$Dir\Agrocast.pem"
$WebServer = "13.203.148.195"
$Slug      = ($ClientName -replace '[^a-zA-Z0-9]', '').ToLower()
$Tmp       = "$env:TEMP\agrocast-adddrone-$Slug"
$script:Failures = @()

. "$Dir\drone-helpers.ps1"
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

function Section($title) { Write-Host ""; Write-Host "=== $title ===" -ForegroundColor Cyan }
function Check($label) {
    if ($LASTEXITCODE -ne 0) {
        $script:Failures += $label
        Write-Host "  !! FAILED: $label (exit $LASTEXITCODE)" -ForegroundColor Red
    } else {
        Write-Host "  OK: $label" -ForegroundColor Green
    }
}

# ---- 1. Work out this drone's number from how many cameras already exist. ----
Section "1/5  Finding the next drone number"
$existingJson = curl.exe -s -m 10 "http://${StreamIP}:5000/get_cameras?apikey=$ApiKey"
try {
    $existing = $existingJson | ConvertFrom-Json
    $existingCount = @($existing).Count
} catch {
    $existingCount = 0
}
$DroneNum = $existingCount + 1
$streamName = Get-DroneStreamName $DroneNum
Write-Host "  Found $existingCount existing camera(s) -> this will be drone $DroneNum (stream '$streamName')" -ForegroundColor Green

# ---- 2. Make sure detection.py on the GPU server actually SUPPORTS --stream, not just
#         that a file happens to exist. A client onboarded before multi-drone support
#         existed (e.g. Client1) already has a detection.py there - but it's the OLD
#         hardcoded-single-camera version, which would silently ignore --stream on drone
#         2+ and try to run drone 1's stream twice. Check for the actual capability
#         (grep for the argparse block), not just file presence. ----
Section "2/5  Confirming detection.py supports multiple drones"
ssh -i $MainKey -o StrictHostKeyChecking=accept-new ubuntu@$GpuIP "grep -q -- '--stream' /home/ubuntu/detection.py 2>/dev/null"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Old single-camera version found (or missing) - uploading the current multi-drone one" -ForegroundColor Yellow
    (Get-Content "$Dir\detection.py") `
        -replace 'EC2_IP\s*=\s*"[^"]*"', "EC2_IP     = `"$StreamIP`"" `
        -replace 'API_KEY\s*=\s*"[^"]*"', "API_KEY     = `"$ApiKey`"" `
        | Set-Content "$Tmp\detection.py"
    scp -i $MainKey -o StrictHostKeyChecking=accept-new "$Tmp\detection.py" ubuntu@${GpuIP}:/home/ubuntu/detection.py
    Check "detection.py uploaded (multi-drone version)"
    # Drone 1's service is ALREADY RUNNING THE OLD CODE IN MEMORY - overwriting the file
    # on disk does nothing to it until it's restarted. Without this, drone 1 would keep
    # running stale code indefinitely after this exact upload.
    ssh -i $MainKey -o StrictHostKeyChecking=accept-new ubuntu@$GpuIP "sudo systemctl restart agrocast-detection && sleep 2 && sudo systemctl is-active agrocast-detection"
    Check "drone 1 service restarted to pick up the new file"
} else {
    Write-Host "  OK: already supports multiple drones" -ForegroundColor Green
}

# ---- 3. New systemd service for this drone. ----
Section "3/5  Installing drone $DroneNum's detection service"
Install-DroneService -GpuIP $GpuIP -MainKey $MainKey -DroneNum $DroneNum -DroneName $DroneName | Out-Null
Check "drone $DroneNum detection service ($(Get-DroneServiceName $DroneNum))"

# ---- 4. This drone's own gimbal script - needs the client's base gimbal_control.py
#         (with EC2_IP/API_KEY already right) to derive from. Re-fetch it fresh from
#         drone 1's hosted copy rather than assume a stale local file. ----
Section "4/5  Hosting drone $DroneNum's gimbal script"
curl.exe -s -m 10 -o "$Tmp\gimbal_control.py" "https://livestreamxk.com/clients/$Slug/gimbal_control.py"
if ((Get-Item "$Tmp\gimbal_control.py" -ErrorAction SilentlyContinue).Length -gt 0) {
    $ok = Publish-DroneGimbalScript -WebKey $WebKey -WebServer $WebServer -Slug $Slug `
        -DroneNum $DroneNum -BaseGimbalControlPath "$Tmp\gimbal_control.py" -Tmp $Tmp
    if (-not $ok) { $LASTEXITCODE = 1 } else { $LASTEXITCODE = 0 }
    Check "drone $DroneNum gimbal script hosted"
} else {
    $script:Failures += "could not fetch drone 1's base gimbal_control.py to derive from"
    Write-Host "  !! could not fetch https://livestreamxk.com/clients/$Slug/gimbal_control.py - is this client fully onboarded?" -ForegroundColor Red
}

# ---- 5. Add the camera. ----
Section "5/5  Adding the camera"
Add-DroneCamera -StreamIP $StreamIP -ApiKey $ApiKey -DroneNum $DroneNum -DroneName $DroneName | Out-Null
Check "camera added (drone $DroneNum)"

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "=== FINISHED WITH $($script:Failures.Count) FAILURE(S) ===" -ForegroundColor Red
    $script:Failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
} else {
    Write-Host "=== ALL DONE - drone $DroneNum added to $ClientName. ===" -ForegroundColor Green
}
Write-Host ""
$subpath = if ($DroneNum -eq 1) { "$Slug" } else { "$Slug/drone$DroneNum" }
Write-Host "New drone's own RC - in Termux (one time only):" -ForegroundColor Yellow
Write-Host "  mkdir -p ~/.termux/boot"
Write-Host "  curl -sf -o ~/.termux/boot/start-gimbal.sh https://livestreamxk.com/clients/$subpath/start-gimbal.sh"
Write-Host "  chmod +x ~/.termux/boot/start-gimbal.sh"
Write-Host "  ~/.termux/boot/start-gimbal.sh"
Write-Host ""
Write-Host "New drone's laptop (manual, can't be scripted):" -ForegroundColor Yellow
Write-Host "  OBS -> Settings -> Stream -> Server: rtmp://${StreamIP}:1935  Key: '$streamName'  -> Start Streaming"
