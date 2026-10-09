# Pinned download sources for install.sh. Never use "latest".
# Verified 2026-10-09 against the official astral-sh/uv release 0.12.19 (published 2026-09-25):
# the sha256 below matches the .sha256 asset, the GitHub release asset digest, and the
# locally computed checksum of the downloaded archive. Python is fetched by this uv version,
# which checks its own embedded checksum for the exact patch version.
UV_VERSION="0.12.19"
UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-apple-darwin.tar.gz"
UV_SHA256="a9a8df1eedeb192f2e47e40e2faabfb387db4b850209118786d42f89dde3e0ba"
PYTHON_VERSION="3.14.7"
