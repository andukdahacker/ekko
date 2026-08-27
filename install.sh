#!/usr/bin/env bash
#
# ekko one-command installer (macOS + Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/andukdahacker/ekko/main/install.sh | bash
#
# What it does:
#   - checks the OS + Python 3.10+
#   - creates an isolated venv at ~/.ekko/venv and installs ekko into it
#   - puts an `ekko` command on your PATH (~/.local/bin)
#   - scaffolds ~/.ekko/config.toml (if missing)
#   - points you at the online-capture prerequisite for your OS
#       macOS 14.4+: none (system-audio tap);  <14.4: BlackHole + `ekko audio setup`
#       Linux: parec (pulseaudio-utils) — no setup, records the sink monitor + mic
#
# Overridable via env vars: EKKO_SPEC (what pip installs), EKKO_HOME, EKKO_BIN, EKKO_REPO.
set -euo pipefail

# --- config -----------------------------------------------------------------
EKKO_REPO="${EKKO_REPO:-https://github.com/andukdahacker/ekko}"
EKKO_SPEC="${EKKO_SPEC:-git+${EKKO_REPO}}"     # pip install target; override to a local path to test
EKKO_HOME="${EKKO_HOME:-$HOME/.ekko}"
EKKO_BIN="${EKKO_BIN:-$HOME/.local/bin}"
VENV="$EKKO_HOME/venv"

# --- pretty logging ---------------------------------------------------------
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else B=; G=; Y=; R=; D=; N=; fi
info() { printf "%s==>%s %s\n" "$G" "$N" "$*"; }
step() { printf "%s • %s%s\n" "$D" "$*" "$N"; }
warn() { printf "%s!! %s%s\n" "$Y" "$*" "$N"; }
die()  { printf "%serror:%s %s\n" "$R" "$N" "$*" >&2; exit 1; }

info "${B}Installing ekko${N}"

# --- 1. platform ------------------------------------------------------------
case "$(uname)" in
  Darwin) OS=macos ;;
  Linux)  OS=linux ;;
  *) die "unsupported OS '$(uname)' — ekko runs on macOS and Linux." ;;
esac
step "platform: $OS"

# --- 2. python 3.10+ --------------------------------------------------------
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  if [ "$OS" = macos ]; then die "need Python 3.10+ on PATH. Install with:  brew install python"
  else die "need Python 3.10+ on PATH. Install e.g.:  sudo apt install python3 python3-venv  (or your distro's package)"; fi
fi
step "using $($PY --version) at $(command -v "$PY")"

# --- 3. isolated venv + install --------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  info "creating venv at $VENV"
  mkdir -p "$EKKO_HOME"
  "$PY" -m venv "$VENV"
else
  step "reusing existing venv at $VENV"
fi
info "installing ekko (this pulls faster-whisper etc; first run is the slow one)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --upgrade "$EKKO_SPEC"

# --- 4. put `ekko` on PATH --------------------------------------------------
mkdir -p "$EKKO_BIN"
ln -sf "$VENV/bin/ekko" "$EKKO_BIN/ekko"
step "linked $EKKO_BIN/ekko -> $VENV/bin/ekko"
case ":$PATH:" in
  *":$EKKO_BIN:"*) : ;;
  *) warn "$EKKO_BIN is not on your PATH. Add this to your shell profile:"
     printf '     %sexport PATH="%s:$PATH"%s\n' "$B" "$EKKO_BIN" "$N" ;;
esac

# --- 5. scaffold config -----------------------------------------------------
CONFIG="$EKKO_HOME/config.toml"
if [ ! -f "$CONFIG" ]; then
  info "writing starter config at $CONFIG"
  cat > "$CONFIG" <<EOF
# ekko config — edit me. See https://github.com/andukdahacker/ekko
data_dir = "~/.ekko"

[capture]
audio_dir = "~/.ekko/audio"
# input_device: unset = default mic (in-person). For online meetings run
# \`ekko audio setup\` and it will set this to "ekko capture".

[whisper]
model_size = "small"      # "small"/"base" = sweet spot; "large-v3" = best, heavier
compute_type = "int8"

[diarize]
enabled = false

[summarize]
provider = "gemini"                       # "gemini" (cloud) or "local" (Ollama, fully offline)
model = "gemini-3.6-flash"
api_key = "PASTE_YOUR_GEMINI_KEY_HERE"    # billing-enabled key; or set GEMINI_API_KEY
# Fully offline instead? Set provider = "local", install Ollama (https://ollama.com),
# run \`ollama pull qwen2.5:7b\`, and set model = "qwen2.5:7b".

[markdown]
enabled = true
vault_dir = "~/Notes/meetings"
EOF
else
  step "keeping existing config at $CONFIG"
fi

# --- 6. online-capture prerequisite -----------------------------------------
ONLINE_STEP=""
if [ "$OS" = macos ]; then
  # macOS 14.4+ captures system audio via a process tap — no BlackHole needed.
  if "$VENV/bin/python" -c 'import sys;from ekko.sources.coreaudio_tap import tap_supported;sys.exit(0 if tap_supported() else 1)' 2>/dev/null; then
    step "online capture uses a system-audio tap — no BlackHole needed."
    ONLINE_STEP="ekko record --kind online   # works out of the box"
  else
    if ! "$VENV/bin/python" - <<'PY' 2>/dev/null
from ekko.sources import coreaudio as ca
raise SystemExit(0 if any("blackhole" in d.name.lower() for d in ca.list_devices()) else 1)
PY
    then
      warn "macOS <14.4: BlackHole (system-audio loopback) needed for ONLINE meetings."
      step "install it:  brew install blackhole-2ch   then:  ekko audio setup"
    fi
    ONLINE_STEP="brew install blackhole-2ch && ekko audio setup"
  fi
else
  # Linux: online capture records the sink monitor + mic in place — no setup,
  # just the `parec` recorder (pulseaudio-utils; works on PipeWire too).
  if ! command -v parec >/dev/null 2>&1; then
    warn "parec not found — needed for ONLINE meetings (PulseAudio/PipeWire)."
    if command -v apt >/dev/null 2>&1;      then step "install:  sudo apt install pulseaudio-utils   (or pipewire-pulse)"
    elif command -v dnf >/dev/null 2>&1;    then step "install:  sudo dnf install pulseaudio-utils   (or pipewire-pulse)"
    elif command -v pacman >/dev/null 2>&1; then step "install:  sudo pacman -S libpulse             (or pipewire-pulse)"
    else step "install your distro's PulseAudio/PipeWire utils (provides parec)"; fi
  else
    step "online capture records the system monitor + mic in place — no setup needed."
  fi
  ONLINE_STEP="ekko record --kind online   # needs parec (pulseaudio-utils)"
fi

# --- done -------------------------------------------------------------------
# Fully-offline summaries are optional (default is Gemini). Tailor the hint to
# whether Ollama is already installed.
if command -v ollama >/dev/null 2>&1; then
  OFFLINE_HINT="Ollama detected — for offline summaries: ${B}ollama pull qwen2.5:7b${N}, then set summarize.provider = local"
else
  OFFLINE_HINT="Prefer fully offline? Install Ollama (https://ollama.com), set summarize.provider = local"
fi

info "${B}Done.${N}"
echo
echo "  1. Add your Gemini key:   ${B}\$EDITOR $CONFIG${N}   (set summarize.api_key)"
echo "  2. Online meetings:       ${B}${ONLINE_STEP}${N}"
echo "  3. Run it:                ${B}ekko${N}   (opens the TUI)"
echo
echo "  ${D}Optional — ${OFFLINE_HINT}${N}"
echo
