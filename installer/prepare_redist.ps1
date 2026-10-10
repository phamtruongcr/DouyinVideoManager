# Tai san cac thanh phan di kem bo cai vao installer\redist (chay mot lan, co the chay lai).
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"

$PyVer = "3.12.10"      # doi neu python.org khong con ban nay
$root  = Join-Path $PSScriptRoot "redist"
New-Item -ItemType Directory -Force $root | Out-Null

function Get-File($url, $dest) {
    if (Test-Path $dest) { Write-Host "Da co: $dest"; return }
    Write-Host "Dang tai $url"
    Invoke-WebRequest -Uri $url -OutFile "$dest.part" -UseBasicParsing
    Move-Item "$dest.part" $dest -Force
}

function Assert-Signed($file, $expect) {
    $sig = Get-AuthenticodeSignature $file
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch $expect) {
        Remove-Item $file -Force
        throw "Chu ky so cua $file khong hop le ($($sig.Status)). Da xoa file, chay lai de tai lai."
    }
    Write-Host "Chu ky hop le: $file"
}

# 1) Microsoft Visual C++ Runtime x64
$vc = Join-Path $root "vc_redist.x64.exe"
Get-File "https://aka.ms/vs/17/release/vc_redist.x64.exe" $vc
Assert-Signed $vc "Microsoft"

# 2) Python (bo cai chinh thuc)
$py = Join-Path $root "python-installer.exe"
Get-File "https://www.python.org/ftp/python/$PyVer/python-$PyVer-amd64.exe" $py
Assert-Signed $py "Python Software Foundation"

# 3) ffmpeg + ffprobe (ban essentials cua gyan.dev)
$ff = Join-Path $root "ffmpeg"
if (-not ((Test-Path "$ff\ffmpeg.exe") -and (Test-Path "$ff\ffprobe.exe"))) {
    $zip = Join-Path $env:TEMP "ffmpeg-essentials.zip"
    $tmp = Join-Path $env:TEMP "ffmpeg-essentials"
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Get-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip
    Expand-Archive $zip -DestinationPath $tmp -Force
    $bin = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    if (-not $bin) { throw "Khong tim thay ffmpeg.exe trong file zip." }
    New-Item -ItemType Directory -Force $ff | Out-Null
    Copy-Item $bin.FullName $ff -Force
    Copy-Item (Join-Path $bin.DirectoryName "ffprobe.exe") $ff -Force
    $lic = Get-ChildItem $tmp -Recurse -Filter LICENSE* | Select-Object -First 1
    if ($lic) { Copy-Item $lic.FullName (Join-Path $ff "LICENSE.txt") -Force }
    Remove-Item $zip -Force
    Remove-Item $tmp -Recurse -Force
}
Write-Host "Da co ffmpeg: $ff"
Write-Host ""
Write-Host "Xong. Thu muc redist da san sang."
