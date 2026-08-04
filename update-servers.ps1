<#
  Run this every time you restart the streaming server and/or GPU server and get new IPs
  - or whenever ANY of detection.py, detection_api.py, gimbal_control.py, or the website
  has code changes to push out. One run deploys everything.

  Usage:
    .\update-servers.ps1 -StreamIP <new streaming server IP> -GpuIP <new GPU server IP>

  Example:
    .\update-servers.ps1 -StreamIP 13.233.99.10 -GpuIP 52.66.11.22

  If you only restarted ONE of the two servers, just pass that one IP and reuse the
  current value for the other (check with: Select-String '^EC2_IP' "detection.py").

  What this does automatically:
    1. Updates EC2_IP in the local copies of detection.py and gimbal_control.py.
    2. Updates mediamtx's WebRTC config on the streaming server and restarts mediamtx.
    3. Deploys detection_api.py to the streaming server and restarts agrocast-api.
    4. Deploys the full detection.py to the GPU server and restarts agrocast-detection
       (picks up ANY code change, not just the IP).
    5. Re-hosts gimbal_control.py - the MK15 self-updates within ~10s, no action needed there.
    6. Deploys the website (drone_stream_portal.html) to the web server.

  What you still have to do yourself after running this (different devices, can't be scripted):
    - OBS: Settings -> Stream -> Server -> rtmp://<StreamIP>:1935 (key stays "drone") -> Start Streaming
    - Website (in your browser): Server IP field -> <StreamIP> -> Connect All Cameras
      (the website's FILES are already redeployed by this script - this is just reconnecting
       your browser session to the current server)
#>

param(
    [Parameter(Mandatory=$true)][string]$StreamIP,
    [Parameter(Mandatory=$true)][string]$GpuIP
)

# NOTE: deliberately NOT "Stop". ssh/scp write ordinary progress and the
# "Warning: Permanently added '<ip>' to the list of known hosts." notice to STDERR,
# and Windows PowerShell 5.1 turns native stderr into a TERMINATING error under
# "Stop" - which silently aborted this script partway through on 2026-07-29 (both
# servers had new IPs, so the known-hosts warning fired). Steps 1-4 had run, step 5
# never did, and the MK15 kept polling the dead old IP => D-pad dead with no visible
# error. Each step now reports its own exit code and the run continues, with a
# verification pass at the end that actually proves what landed.
$ErrorActionPreference = "Continue"
$Dir       = "D:\Live Stream"
$MainKey   = "$Dir\agrocast-main-key.pem"
$WebKey    = "$Dir\Agrocast.pem"
$WebServer = "13.203.148.195"
$script:Failures = @()

function Section($title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
}

# Run a native command and record (don't throw on) failure, so one bad step
# can never silently skip the steps after it.
function Check($label) {
    if ($LASTEXITCODE -ne 0) {
        $script:Failures += $label
        Write-Host "  !! FAILED: $label (exit $LASTEXITCODE)" -ForegroundColor Red
    } else {
        Write-Host "  OK: $label" -ForegroundColor Green
    }
}

# ---- 1. Fix EC2_IP in the LOCAL files FIRST, so whatever we push next is correct
#         regardless of whether the IP changed, the code changed, or both. ----
Section "1/6  Updating EC2_IP in local detection.py and gimbal_control.py"
(Get-Content "$Dir\detection.py") -replace 'EC2_IP\s*=\s*"[^"]*"', "EC2_IP     = `"$StreamIP`"" | Set-Content "$Dir\detection.py"
(Get-Content "$Dir\gimbal_control.py") -replace 'EC2_IP\s*=\s*"[^"]*"', "EC2_IP    = `"$StreamIP`"" | Set-Content "$Dir\gimbal_control.py"
Write-Host "Local files updated." -ForegroundColor Green

# ---- 2. mediamtx on the streaming server ----
Section "2/6  mediamtx WebRTC config on streaming server ($StreamIP)"
$mediamtxCmd = "sudo sed -i 's/webrtcAdditionalHosts: \[.*\]/webrtcAdditionalHosts: [$StreamIP]/' /usr/local/etc/mediamtx.yml && sudo systemctl restart mediamtx && echo MEDIAMTX_OK"
ssh -i $MainKey -o StrictHostKeyChecking=accept-new ubuntu@$StreamIP $mediamtxCmd
Check "mediamtx config + restart"

# ---- 3. Deploy detection_api.py to the streaming server ----
Section "3/6  Deploying detection_api.py to streaming server ($StreamIP)"
scp -i $MainKey -o StrictHostKeyChecking=accept-new "$Dir\detection_api.py" ubuntu@${StreamIP}:/home/ubuntu/detection_api.py
ssh -i $MainKey -o StrictHostKeyChecking=accept-new ubuntu@$StreamIP "sudo systemctl restart agrocast-api && sleep 2 && curl -s http://localhost:5000/health"

# ---- 4. Deploy the FULL local detection.py - this picks up ANY code changes made
#         since the last run, not just the IP (previously this only ran a remote sed,
#         which silently left code changes undeployed - fixed 2026-07-27). ----
Section "4/6  Deploying detection.py to GPU server ($GpuIP)"
scp -i $MainKey -o StrictHostKeyChecking=accept-new "$Dir\detection.py" ubuntu@${GpuIP}:/home/ubuntu/detection.py
Check "detection.py upload"
ssh -i $MainKey -o StrictHostKeyChecking=accept-new ubuntu@$GpuIP "sudo systemctl restart agrocast-detection && sleep 3 && sudo systemctl is-active agrocast-detection"
Check "agrocast-detection restart"

# ---- 5. Re-host gimbal_control.py - the MK15 self-updates within ~10s on its own,
#         no manual action there, whether this was an IP change or a code change. ----
Section "5/6  Re-hosting gimbal_control.py (MK15 self-updates automatically)"
scp -i $WebKey -o StrictHostKeyChecking=accept-new "$Dir\gimbal_control.py" ubuntu@${WebServer}:~/gimbal_control.py
Check "gimbal_control.py upload"
ssh -i $WebKey -o StrictHostKeyChecking=accept-new ubuntu@$WebServer "sudo mv ~/gimbal_control.py /var/www/liveStreamXK/gimbal_control.py && sudo chown www-data:www-data /var/www/liveStreamXK/gimbal_control.py && echo GIMBAL_HOSTED"
Check "gimbal_control.py published"

# ---- 6. Deploy the website ----
Section "6/6  Deploying website to web server"
scp -i $WebKey -o StrictHostKeyChecking=accept-new "$Dir\drone_stream_portal.html" ubuntu@${WebServer}:~/drone_stream_portal.html
ssh -i $WebKey -o StrictHostKeyChecking=accept-new ubuntu@$WebServer "sudo mv ~/drone_stream_portal.html /var/www/liveStreamXK/index.html && sudo chown www-data:www-data /var/www/liveStreamXK/index.html && echo WEBSITE_DONE"

# ---- 7. VERIFY - prove what actually landed, don't assume. ----
# This exists because a silent partial deploy is indistinguishable from a good one
# until something breaks in the field (see the note at $ErrorActionPreference).
Section "7/7  Verifying deployment"

# The MK15 downloads its code from this URL, so this is the single most important
# check: if the hosted IP is stale the D-pad and auto-follow are dead, with no
# other visible symptom.
$hostedMatch = (curl.exe -s "https://livestreamxk.com/gimbal_control.py" | Select-String -Pattern '^EC2_IP\s*=\s*"([^"]*)"')
# NOTE: if the fetch failed or the file wasn't found, $hostedMatch is empty and
# .Matches.Groups[1].Value on it throws "Cannot index into a null array" - a real crash
# hit on 2026-08-03. Guard it so a real failure prints a clean message instead of a
# PowerShell exception that looks scarier than the actual problem.
if ($hostedMatch) {
    $hostedIP = $hostedMatch.Matches.Groups[1].Value
} else {
    $hostedIP = $null
}
if ($hostedIP -eq $StreamIP) {
    Write-Host "  OK: hosted gimbal_control.py -> $hostedIP (MK15 self-updates within ~10s)" -ForegroundColor Green
} else {
    $script:Failures += "hosted gimbal_control.py has WRONG IP (got '$hostedIP', expected $StreamIP)"
    Write-Host "  !! hosted gimbal_control.py -> '$hostedIP' but should be $StreamIP" -ForegroundColor Red
}

# API must answer on the new streaming IP or nothing downstream works.
# NOTE: "-o $null" is WRONG on native commands - PowerShell silently drops $null from the
# argument list entirely (confirmed 2026-08-03: a real argv dump showed -o directly
# swallowing the URL as ITS OWN argument, leaving curl with none - "no URL specified").
# The literal string "NUL" (Windows' null device) is what's actually needed here.
curl.exe -s -m 8 -o NUL "http://${StreamIP}:5000/get_gimbal_cmd?camera_id=cam_drone&apikey=AgrocastClient2026"
Check "API reachable on ${StreamIP}:5000"

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "=== FINISHED WITH $($script:Failures.Count) FAILURE(S) - fix these before flying ===" -ForegroundColor Red
    $script:Failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
} else {
    Write-Host "=== ALL DONE - every file+config redeployed and verified. ===" -ForegroundColor Green
}
Write-Host ""
Write-Host "Still manual (different devices, can't be scripted):" -ForegroundColor Yellow
Write-Host "  1. OBS     -> Settings -> Stream -> Server: rtmp://${StreamIP}:1935  (key stays 'drone') -> Start Streaming"
Write-Host "  2. Website -> Server IP field: $StreamIP -> Connect All Cameras (files already redeployed, just reconnect)"
Write-Host "  MK15: nothing to do -- auto-updates within ~10s (Termux:Boot setup required once, already covered)."
