#!/usr/bin/env bash
# Sourced by install.sh only when its automatic configuration was requested.
# Build a private compatible player; do not overwrite distro/custom binaries.
bluealsa_prefix=/opt/bureau-audio-tools/bluealsa-4.3.1
if [[ -x "$bluealsa_prefix/bin/bluealsa-aplay" ]]; then
  export PATH="$bluealsa_prefix/bin:$PATH"
fi
if { ! command -v bluealsa >/dev/null && ! command -v bluealsad >/dev/null; } || \
   ! command -v bluealsa-aplay >/dev/null || [[ $(bluealsa-aplay --help 2>&1) != *--volume* ]]; then
  echo 'Construction de BlueALSA 4.3.1 (SBC) pour le lecteur à volume logiciel…'
  apt-get install -y build-essential autoconf automake libtool pkg-config curl ca-certificates \
    libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev libsbc-dev
  bluealsa_work=$(mktemp -d)
  if ! (
    set -e
    trap 'rm -rf "$bluealsa_work"' EXIT
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --retry 3 \
      https://codeload.github.com/arkq/bluez-alsa/tar.gz/refs/tags/v4.3.1 -o "$bluealsa_work/source.tar.gz" || exit 1
    echo "933fe898dfac21fdfeb5f4ffa685c2aa2db9c064d639170ac2652f156e956a2a  $bluealsa_work/source.tar.gz" | sha256sum --check || exit 1
    tar -xzf "$bluealsa_work/source.tar.gz" -C "$bluealsa_work" || exit 1
    cd "$bluealsa_work/bluez-alsa-4.3.1" || exit 1
    autoreconf --install || exit 1
    ./configure --prefix="$bluealsa_prefix" --localstatedir=/var \
      --with-alsaplugindir="$bluealsa_prefix/lib/alsa-lib" \
      --with-alsaconfdir="$bluealsa_prefix/etc/alsa/conf.d" \
      --with-dbusconfdir="$bluealsa_prefix/share/dbus-1/system.d" --enable-cli || exit 1
    make -j2 || exit 1
    make install || exit 1
  ); then
    rm -rf "$bluealsa_work"
    echo 'Échec de compilation BlueALSA. Aucun lecteur existant n’a été arrêté.' >&2
    exit 1
  fi
  rm -rf "$bluealsa_work"
  export PATH="$bluealsa_prefix/bin:$PATH"
fi
