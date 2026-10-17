#!/usr/bin/env bash
# GitHub-built native installer. Production only; no Python, gh, or local build required.
set -euo pipefail
repo="pengchengluoyi/MinoNexus"
case "$(uname -s)" in Darwin) os=darwin;; Linux) os=linux;; *) echo 'Windows: use install.ps1 or the installer EXE.' >&2; exit 1;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64;; x86_64|amd64) arch=x64;; *) echo 'Unsupported CPU architecture' >&2; exit 1;; esac
if [[ "$os-$arch" == linux-arm64 ]]; then echo 'This release currently supports Linux x64.' >&2; exit 1; fi
name="MinoInstaller-$os-$arch"
base="https://github.com/$repo/releases/latest/download"
folder="$(mktemp -d "${TMPDIR:-/tmp}/mino-install.XXXXXX")"
echo "Downloading GitHub-built installer: $name"
curl --fail --location --show-error --proto '=https' --proto-redir '=https' "$base/INSTALLER-SHA256SUMS.txt" -o "$folder/checksums.txt"
curl --fail --location --show-error --proto '=https' --proto-redir '=https' "$base/$name" -o "$folder/$name"
expected="$(awk -v name="$name" '$2 == name {print $1}' "$folder/checksums.txt")"
[[ "$expected" =~ ^[a-f0-9]{64}$ ]] || { echo 'Missing or invalid installer checksum' >&2; exit 1; }
if command -v sha256sum >/dev/null; then actual="$(sha256sum "$folder/$name" | awk '{print $1}')"; else actual="$(shasum -a 256 "$folder/$name" | awk '{print $1}')"; fi
[[ "$actual" == "$expected" ]] || { echo 'Installer checksum mismatch; files preserved.' >&2; exit 1; }
chmod +x "$folder/$name"
"$folder/$name" --components nexus "$@"
