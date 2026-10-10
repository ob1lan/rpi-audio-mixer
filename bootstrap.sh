#!/bin/sh
# Download a complete repository snapshot before running anything privileged.
set -eu
umask 077
repo=${BUREAU_INSTALL_REPO:-ob1lan/rpi-audio-mixer}
ref=${BUREAU_INSTALL_REF:-main}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo|--ref)
      [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
      if [ "$1" = --repo ]; then repo=$2; else ref=$2; fi
      shift 2 ;;
    --help)
      echo 'Usage: sh bootstrap.sh [--repo OWNER/REPO] [--ref BRANCH|TAG|SHA] [--non-interactive] [--output CARD:DEVICE]'
      echo 'GITHUB_TOKEN or GH_TOKEN may supply read access to a private repository.'
      exit 0 ;;
    *) break ;;
  esac
done
case "$repo" in ''|*[!A-Za-z0-9_./-]*|*..*|/*|*/|*/*/*) echo 'Invalid GitHub repository.' >&2; exit 2 ;; esac
case "$repo" in */*) ;; *) echo 'Use OWNER/REPO.' >&2; exit 2 ;; esac
case "$ref" in ''|*[!A-Za-z0-9_./-]*|*..*|/*) echo 'Invalid Git reference.' >&2; exit 2 ;; esac
[ "$(uname -s)" = Linux ] || { echo 'Run this on the Raspberry Pi (Debian/Raspberry Pi OS).' >&2; exit 1; }
for executable in curl tar python3 bash; do
  command -v "$executable" >/dev/null || { echo "Required command missing: $executable" >&2; exit 1; }
done
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "Downloading $repo ($ref)…"
token=${GH_TOKEN:-${GITHUB_TOKEN:-}}
if [ -n "$token" ]; then
  # Keep credentials out of curl's process arguments and downloaded files.
  case "$token" in *[!A-Za-z0-9_]*) echo 'Invalid GitHub token format.' >&2; exit 2 ;; esac
  printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github+json"\n' "$token" |
    curl --config - --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --retry 3 \
      "https://api.github.com/repos/$repo/tarball/$ref" -o "$work/repo.tar.gz"
else
  if ! curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --retry 3 \
    "https://api.github.com/repos/$repo/tarball/$ref" -o "$work/repo.tar.gz"; then
    echo 'Download failed. Check the repository/ref; a private repository requires GH_TOKEN with read access.' >&2
    exit 1
  fi
fi
# Reject traversal, links and special files before extracting or using sudo.
python3 - "$work/repo.tar.gz" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath
with tarfile.open(sys.argv[1]) as archive:
    members = archive.getmembers()
    roots = set()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts or not (member.isfile() or member.isdir()):
            raise SystemExit('Unsafe repository archive.')
        roots.add(path.parts[0])
    if len(roots) != 1 or not members:
        raise SystemExit('Invalid repository archive.')
PY
mkdir "$work/repo"
tar -xzf "$work/repo.tar.gz" -C "$work/repo" --strip-components=1 --no-same-owner
for file in install.sh configure.py prepare-bluealsa.sh app.py audio.py; do
  [ -f "$work/repo/$file" ] || { echo "Incomplete repository: $file" >&2; exit 1; }
done
# The application never needs the repository credential.
unset token GH_TOKEN GITHUB_TOKEN
if [ "$(id -u)" = 0 ]; then
  bash "$work/repo/install.sh" --upgrade --configure "$@"
else
  command -v sudo >/dev/null || { echo 'sudo is required; alternatively run as root.' >&2; exit 1; }
  sudo bash "$work/repo/install.sh" --upgrade --configure "$@"
fi
