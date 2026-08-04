<#
  Sets up a brand-new client on their own pair of servers, in one run.

  WHAT YOU DO MANUALLY FIRST (this part stays manual on purpose - it's a few AWS
  console clicks and there's no good reason to script AWS account/billing actions):
    1. EC2 -> Launch Instances -> streaming server. Ubuntu Server, t3.medium,
       security group open on 22/1935/5000/8554/8889, use our existing key pair
       (agrocast-main-key.pem). Note its IP.
    2. EC2 -> Launch Instances -> GPU server. A Deep Learning AMI (PyTorch), g4dn.xlarge,
       security group open on 22 only, same key pair. Note its IP.
    3. (Recommended) EC2 -> Elastic IPs -> allocate one for each, associate them, so
       these IPs never change again. Then use THOSE IPs below.

  WHAT THIS SCRIPT DOES FOR YOU (everything after the servers exist):
    1. Generates a brand-new random API key for this client (you never have to invent one).
    2. Installs mediamtx (video relay) + our backend on the streaming server, as an
       auto-starting service.
    3. Installs our AI packages + detection code on the GPU server, as an auto-starting
       service (assumes the Deep Learning AMI already has PyTorch at /opt/pytorch - it does
       on the AMI we already use, see reference notes).
    4. Hosts a client-specific gimbal script at its own private URL, so their RC points
       at THEIR script, not ours.
    5. Adds a first camera automatically, so there's nothing left to click before testing.
    6. Prints exactly what to hand the client: server IP, API key, gimbal URL.

  Usage:
    .\onboard-client.ps1 -ClientName acmefarms -StreamIP <their streaming IP> -GpuIP <their GPU IP>
    .\onboard-client.ps1 -ClientName acmefarms -StreamIP <ip> -GpuIP <ip> -NumDrones 3

  -NumDrones (default 1): sets up that many drones at once, each as its own detection.py
  process/systemd service, each added as its own camera on the website. Drone 1 always
  uses stream "drone" / service "agrocast-detection" (matching every client onboarded
  before this existed, so nothing about drone 1 changes). Drones 2+ use "drone2",
  "drone3", etc. To add ONE MORE drone to an already-onboarded client later, use
  add-drone.ps1 instead of re-running this.

  IMPORTANT - the single-drone path (no -NumDrones) has been proven end-to-end on a real
  client (2026-08-03, after finding and fixing 3 real bugs along the way - see
  project_agrocast_status memory). The -NumDrones>1 path is new and has NOT yet been
  proven on real hardware - the underlying pieces (parameterized detection.py, the
  per-drone systemd unit, the add_camera call) are each individually verified, but a
  fresh multi-drone run start-to-finish has not been watched yet. Watch it closely the
  first time, the same way the single-drone path was proven.
#>

param(
    [Parameter(Mandatory=$true)][string]$ClientName,
    [Parameter(Mandatory=$true)][string]$StreamIP,
    [Parameter(Mandatory=$true)][string]$GpuIP,
    [int]$NumDrones = 1
)

$ErrorActionPreference = "Continue"   # see update-servers.ps1's note on why not "Stop"
$Dir       = "D:\Live Stream"
$MainKey   = "$Dir\agrocast-main-key.pem"
$WebKey    = "$Dir\Agrocast.pem"
$WebServer = "13.203.148.195"
$Slug      = ($ClientName -replace '[^a-zA-Z0-9]', '').ToLower()
$Tmp       = "$env:TEMP\agrocast-onboard-$Slug"
$script:Failures = @()

. "$Dir\drone-helpers.ps1"
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

function Section($title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
}
function Check($label) {
    if ($LASTEXITCODE -ne 0) {
        $script:Failures += $label
        Write-Host "  !! FAILED: $label (exit $LASTEXITCODE)" -ForegroundColor Red
    } else {
        Write-Host "  OK: $label" -ForegroundColor Green
    }
}

# ---- 0. Generate this client's own API key. This is the answer to "how is it
#         generated" - a random string, made fresh right here, never reused across
#         clients and never the same as our own testing key. ----
Section "0/6  Generating a new API key for $ClientName"
$chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
$ApiKey = -join ((1..28) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
Write-Host "  Generated key: $ApiKey" -ForegroundColor Green

# ---- 1. Build this client's own copies of the three code files, with their key
#         and IPs substituted in. The LOCAL files we use for our own testing are
#         never touched - these are throwaway copies in $Tmp. ----
Section "1/6  Preparing $ClientName's copies of detection_api.py / detection.py / gimbal_control.py"

(Get-Content "$Dir\detection_api.py") `
    -replace 'API_KEY\s*=\s*"[^"]*"', "API_KEY = `"$ApiKey`"" `
    | Set-Content "$Tmp\detection_api.py"

(Get-Content "$Dir\detection.py") `
    -replace 'EC2_IP\s*=\s*"[^"]*"', "EC2_IP     = `"$StreamIP`"" `
    -replace 'API_KEY\s*=\s*"[^"]*"', "API_KEY     = `"$ApiKey`"" `
    | Set-Content "$Tmp\detection.py"

(Get-Content "$Dir\gimbal_control.py") `
    -replace 'EC2_IP\s*=\s*"[^"]*"', "EC2_IP    = `"$StreamIP`"" `
    -replace 'API_KEY\s*=\s*"[^"]*"', "API_KEY   = `"$ApiKey`"" `
    | Set-Content "$Tmp\gimbal_control.py"

Write-Host "  Client-specific files written to $Tmp" -ForegroundColor Green

# ---- 2. Streaming server: mediamtx + backend, from scratch. ----
Section "2/6  Setting up streaming server ($StreamIP)"

$streamSetup = @'
set -e
sudo apt update -y -qq
sudo apt install -y -qq python3 python3-pip curl >/dev/null
# --break-system-packages: newer Ubuntu (26.04+, Python 3.14) enforces PEP 668 and
# refuses a plain "pip3 install" system-wide with "error: externally-managed-environment".
# Confirmed 2026-08-03: this single line aborted the ENTIRE rest of this script under
# "set -e" - mediamtx was never installed, no systemd units were created, nothing
# started. Safe here since this is a single-purpose dedicated server with nothing else
# using system Python.
# Checked detection_api.py's actual imports (2026-08-03) rather than assume a package
# list from memory - it also needs flask_cors (pip name "flask-cors", hyphenated - the
# import name has an underscore, a common PyPI naming gotcha). Missing it was the exact
# cause of agrocast-api crash-looping on the first real onboarding run.
pip3 install --quiet --break-system-packages flask flask-cors requests

if [ ! -f /usr/local/bin/mediamtx ]; then
  cd /tmp
  URL=$(curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest | grep -oP "\"browser_download_url\":\s*\"\K[^\"]*linux_amd64\.tar\.gz(?=\")" | head -1)
  curl -sL -o mediamtx.tar.gz "$URL"
  tar -xzf mediamtx.tar.gz
  sudo mv mediamtx /usr/local/bin/mediamtx
  sudo mv mediamtx.yml /usr/local/etc/mediamtx.yml
fi
sudo sed -i "s/webrtcAdditionalHosts: \[.*\]/webrtcAdditionalHosts: [STREAMIP_PLACEHOLDER]/" /usr/local/etc/mediamtx.yml

sudo tee /etc/systemd/system/mediamtx.service >/dev/null <<'EOF'
[Unit]
Description=mediamtx
After=network.target
[Service]
ExecStart=/usr/local/bin/mediamtx /usr/local/etc/mediamtx.yml
Restart=always
User=root
[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/agrocast-api.service >/dev/null <<'EOF'
[Unit]
Description=Agrocast API backend
After=network.target
[Service]
ExecStart=/usr/bin/python3 /home/ubuntu/detection_api.py
WorkingDirectory=/home/ubuntu
Restart=always
User=ubuntu
[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
sudo systemctl enable --now agrocast-api
echo STREAM_SETUP_OK
'@ -replace 'STREAMIP_PLACEHOLDER', $StreamIP

scp -i $MainKey -o StrictHostKeyChecking=accept-new "$Tmp\detection_api.py" ubuntu@${StreamIP}:/home/ubuntu/detection_api.py
Check "detection_api.py uploaded"
$streamSetup | ssh -i $MainKey -o StrictHostKeyChecking=accept-new ubuntu@$StreamIP "bash -s"
Check "streaming server installed + services started"

# ---- 3. GPU server: AI packages + detection code, from scratch. ----
Section "3/6  Setting up GPU server ($GpuIP)"

# Just the packages here - the systemd service(s) are created per-drone below via
# Install-DroneService, since detection.py is now a SINGLE generic file (takes --stream
# as a command-line arg) shared by every drone this client has, not one file per drone.
$gpuSetup = @'
set -e
sudo apt update -y -qq
sudo apt install -y -qq ffmpeg >/dev/null
/opt/pytorch/bin/pip install --quiet opencv-python supervision ultralytics requests torchreid
echo GPU_SETUP_OK
'@

scp -i $MainKey -o StrictHostKeyChecking=accept-new "$Tmp\detection.py" ubuntu@${GpuIP}:/home/ubuntu/detection.py
Check "detection.py uploaded"
$gpuSetup | ssh -i $MainKey -o StrictHostKeyChecking=accept-new ubuntu@$GpuIP "bash -s"
Check "GPU server packages installed"

for ($i = 1; $i -le $NumDrones; $i++) {
    $svcName = Get-DroneServiceName $i
    Install-DroneService -GpuIP $GpuIP -MainKey $MainKey -DroneNum $i | Out-Null
    Check "drone $i detection service ($svcName)"
}

# ---- 4. Host EACH drone's own gimbal script at its own private URL - each drone is a
#         separate aircraft with its own RC, so each needs its own CAMERA_ID baked in
#         (gimbal_control.py hardcodes this the same way detection.py used to). Drone 1
#         keeps the original un-suffixed path so already-onboarded clients are unaffected. ----
Section "4/6  Hosting gimbal script(s) for $NumDrones drone(s)"
for ($i = 1; $i -le $NumDrones; $i++) {
    $ok = Publish-DroneGimbalScript -WebKey $WebKey -WebServer $WebServer -Slug $Slug `
        -DroneNum $i -BaseGimbalControlPath "$Tmp\gimbal_control.py" -Tmp $Tmp
    if (-not $ok) { $LASTEXITCODE = 1 } else { $LASTEXITCODE = 0 }
    Check "drone $i gimbal script hosted"
}

# ---- 5. Add a camera for each drone automatically, so there's nothing left to click. ----
Section "5/6  Adding $NumDrones camera(s)"
Start-Sleep -Seconds 3   # give agrocast-api a moment to finish starting
for ($i = 1; $i -le $NumDrones; $i++) {
    Add-DroneCamera -StreamIP $StreamIP -ApiKey $ApiKey -DroneNum $i | Out-Null
    Check "camera $i added (Camera $i)"
}

# ---- 6. VERIFY - prove what actually landed, don't assume. ----
Section "6/6  Verifying"
# NOTE: "-o $null" is WRONG on native commands - PowerShell silently drops $null from the
# argument list entirely (confirmed 2026-08-03 via a real argv dump: -o ends up directly
# swallowing the URL as ITS OWN argument, leaving curl with none - "no URL specified").
# The literal string "NUL" (Windows' null device) is what actually works.
curl.exe -s -m 8 -o NUL "http://${StreamIP}:5000/get_config?apikey=$ApiKey"
Check "API answers with the new key on ${StreamIP}:5000"

for ($i = 1; $i -le $NumDrones; $i++) {
    $subpath = if ($i -eq 1) { "$Slug" } else { "$Slug/drone$i" }
    $hostedMatch = (curl.exe -s "https://livestreamxk.com/clients/$subpath/gimbal_control.py" | Select-String -Pattern '^EC2_IP\s*=\s*"([^"]*)"')
    # Guard against no match (file missing/unreachable) - indexing .Matches on an empty
    # result crashes with "Cannot index into a null array" (hit for real 2026-08-03), which
    # reads as a script bug rather than the actual upstream failure it was masking.
    if ($hostedMatch) {
        $hostedIP = $hostedMatch.Matches.Groups[1].Value
    } else {
        $hostedIP = $null
    }
    if ($hostedIP -eq $StreamIP) {
        Write-Host "  OK: drone $i hosted gimbal_control.py -> $hostedIP" -ForegroundColor Green
    } else {
        $script:Failures += "drone $i hosted gimbal_control.py has wrong IP"
        Write-Host "  !! drone $i hosted gimbal_control.py -> $hostedIP but should be $StreamIP" -ForegroundColor Red
    }
}

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "=== FINISHED WITH $($script:Failures.Count) FAILURE(S) - fix these before handing off ===" -ForegroundColor Red
    $script:Failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
} else {
    Write-Host "=== ALL DONE - $ClientName is set up. ===" -ForegroundColor Green
}
Write-Host ""
Write-Host "Give the client:" -ForegroundColor Yellow
Write-Host "  Website:    https://livestreamxk.com"
Write-Host "  Server IP:  $StreamIP"
Write-Host "  API Key:    $ApiKey"
Write-Host ""
Write-Host "EACH drone has its own RC and its own laptop - repeat this on every one of them" -ForegroundColor Yellow
Write-Host "(first time only; skip the sideload part if that RC already has Termux + Termux:Boot):"
Write-Host "  Sideload Termux and Termux:Boot from their GitHub release pages (NOT the Play"
Write-Host "  Store version of Termux - it doesn't work with F-Droid's Termux:Boot). Open"
Write-Host "  Termux once to let it finish its first-time setup."
Write-Host ""
for ($i = 1; $i -le $NumDrones; $i++) {
    $subpath = if ($i -eq 1) { "$Slug" } else { "$Slug/drone$i" }
    $sn = Get-DroneStreamName $i
    Write-Host "Drone ${i} RC - in Termux (one time only, makes it start on every power-on):" -ForegroundColor Yellow
    Write-Host "  mkdir -p ~/.termux/boot"
    Write-Host "  curl -sf -o ~/.termux/boot/start-gimbal.sh https://livestreamxk.com/clients/$subpath/start-gimbal.sh"
    Write-Host "  chmod +x ~/.termux/boot/start-gimbal.sh"
    Write-Host "  ~/.termux/boot/start-gimbal.sh   # starts it right now, without waiting for a reboot"
    Write-Host ""
}
Write-Host "From then on each one self-updates and auto-starts on its own - nothing further to do."
Write-Host ""
Write-Host "Still manual (each drone's laptop, can't be scripted) - use that drone's own Stream Key:" -ForegroundColor Yellow
for ($i = 1; $i -le $NumDrones; $i++) {
    $sn = Get-DroneStreamName $i
    Write-Host "  Drone ${i}: OBS -> Settings -> Stream -> Server: rtmp://${StreamIP}:1935  Key: '$sn'  -> Start Streaming"
}
