# GitHub-built native installer. Production only; no Python, gh, or local build required.
$ErrorActionPreference = 'Stop'
$repo = 'pengchengluoyi/MinoNexus'
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'This release supports Windows x64.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$name = 'MinoInstaller-win32-x64.exe'
$base = "https://github.com/$repo/releases/latest/download"
$folder = Join-Path ([IO.Path]::GetTempPath()) ('mino-install-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $folder | Out-Null
$checksums = Join-Path $folder 'checksums.txt'
$installer = Join-Path $folder $name
Invoke-WebRequest -UseBasicParsing -Uri "$base/INSTALLER-SHA256SUMS.txt" -OutFile $checksums
Invoke-WebRequest -UseBasicParsing -Uri "$base/$name" -OutFile $installer
$matches = @(Get-Content $checksums | Where-Object { ($_ -split '\s+')[1] -eq $name })
if ($matches.Count -ne 1) { throw 'Missing or ambiguous installer checksum' }
$expected = ($matches[0] -split '\s+')[0]
if ($expected -notmatch '^[a-f0-9]{64}$' -or (Get-FileHash $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw 'Installer checksum mismatch; files preserved.' }
& $installer --components nexus @args
if ($LASTEXITCODE -ne 0) { throw "Mino installer exited with code $LASTEXITCODE; files preserved at $folder" }
