# Creates the YaqaLab code-signing certificate, once, in this Windows user's certificate store.
#
#   powershell -ExecutionPolicy Bypass -File tools\audio-server\native\installer\make-cert.ps1
#
# Self-signed: the program and the installer carry a real signature by "YaqaLab" (Properties >
# Digital Signatures) that proves they were not changed after signing, but Windows does not trust
# the signer, so SmartScreen still warns. The private key is made non-exportable and never leaves
# this store; only the public part is written (dist\yaqalab-codesign.cer), for anyone who chooses to
# trust it on their own machine.
$ErrorActionPreference = "Stop"
$subject = "CN=YaqaLab"
$dist = Join-Path $PSScriptRoot "..\dist"
New-Item -ItemType Directory -Force $dist | Out-Null

$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $_.Subject -eq $subject -and $_.NotAfter -gt (Get-Date) -and $_.HasPrivateKey } |
    Sort-Object NotAfter -Descending | Select-Object -First 1
if ($cert) {
    "Already there: $($cert.Thumbprint), valid until $($cert.NotAfter.ToString('yyyy-MM-dd'))"
} else {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject `
        -FriendlyName "YaqaLab code signing (self-signed)" `
        -CertStoreLocation Cert:\CurrentUser\My `
        -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 `
        -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddYears(10)
    "Made: $($cert.Thumbprint), valid until $($cert.NotAfter.ToString('yyyy-MM-dd'))"
}
Export-Certificate -Cert $cert -FilePath (Join-Path $dist "yaqalab-codesign.cer") | Out-Null
"Public part: $(Resolve-Path (Join-Path $dist 'yaqalab-codesign.cer'))"
