#!/bin/sh
# Stage the port (no Gradle) and lay it out as a simulated device.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
export BUILD_WORK="${BUILD_WORK:-$(pwd)/build-work}"
python3 "$HERE/../build_android.py" --stage-only
python3 "$HERE/simulate_device.py" package
