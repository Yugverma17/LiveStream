<?php
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, X-Api-Key');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { exit(0); }

$target = $_GET['target'] ?? '';
$path   = $_GET['path'] ?? '';
$apikey = $_GET['apikey'] ?? '';
$port   = $_GET['port'] ?? '5000';

if (!$target || !$path) {
    http_response_code(400);
    echo json_encode(['error' => 'target and path required']);
    exit;
}

if (!filter_var($target, FILTER_VALIDATE_IP)) {
    http_response_code(400);
    echo json_encode(['error' => 'invalid target IP']);
    exit;
}

if (!ctype_digit((string)$port) || (int)$port < 1 || (int)$port > 65535) {
    http_response_code(400);
    echo json_encode(['error' => 'invalid port']);
    exit;
}

// Build URL — include apikey as query param so Flask can validate.
// port defaults to 5000 (the Flask API); the WebRTC/WHEP video path uses
// port=8889 (mediamtx) instead, so this same proxy works for both.
$url = "http://{$target}:{$port}/{$path}";
if ($apikey) {
    $url .= (strpos($path, '?') !== false ? '&' : '?') . "apikey={$apikey}";
}

$method = $_SERVER['REQUEST_METHOD'];
$ch = curl_init($url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_TIMEOUT, 12); // WHEP handshakes (STUN gather + mediamtx's own 10s handshake timeout) need more than 5s

if ($method === 'POST') {
    $body = file_get_contents('php://input');
    // Forward the caller's actual Content-Type (application/json for the API,
    // application/sdp for WHEP) instead of assuming JSON for everything.
    $contentType = $_SERVER['CONTENT_TYPE'] ?? 'application/json';
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    curl_setopt($ch, CURLOPT_HTTPHEADER, [
        'Content-Type: ' . $contentType,
        'X-Api-Key: ' . $apikey
    ]);
}

$response = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$upstreamContentType = curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
$curlError = curl_error($ch);
curl_close($ch);

if ($curlError) {
    http_response_code(502);
    echo json_encode(['error' => 'Cannot reach server: ' . $curlError]);
    exit;
}

http_response_code($httpCode);
// Pass through the real upstream Content-Type (application/sdp for WHEP
// answers) instead of forcing application/json on every response.
header('Content-Type: ' . ($upstreamContentType ?: 'application/json'));
echo $response;
