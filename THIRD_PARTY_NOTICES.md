# Third-party notices

ditl_sbp itself is covered by `LICENSE`. The components below are **not** part of ditl_sbp and are **not** included in the release ZIP: `install.sh` downloads them from their official sources onto your computer, where they remain under their own licenses. Each package's full license text is installed with it (in its `*.dist-info` folder inside `~/.local/share/ditl-sbp/current/.venv`).

## Runtime fetched by the installer

| Component | Version | License | Source |
|---|---|---|---|
| uv | 0.12.19 | MIT OR Apache-2.0 | https://github.com/astral-sh/uv |
| CPython (python-build-standalone build, via uv) | 3.14.7 | PSF-2.0, plus the licenses of the libraries bundled in the build | https://github.com/astral-sh/python-build-standalone |

## Python packages (`requirements.lock`)

| Package | Version | License | Source |
|---|---|---|---|
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anthropic | 0.107.1 | MIT | https://github.com/anthropics/anthropic-sdk-python |
| anyio | 4.15.1 | MIT | https://github.com/agronholm/anyio |
| attrs | 26.1.0 | MIT | https://github.com/python-attrs/attrs |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| charset-normalizer | 3.5.1 | see project | https://github.com/jawah/charset_normalizer |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| distro | 1.9.0 | Apache-2.0 | https://github.com/python-distro/distro |
| docstring_parser | 0.18.0 | MIT | https://github.com/rr-/docstring_parser |
| et_xmlfile | 2.0.0 | MIT | https://foss.heptapod.net/openpyxl/et_xmlfile |
| google-auth | 2.58.0 | Apache-2.0 | https://github.com/googleapis/google-cloud-python/tree/main/packages/google-auth |
| google-genai | 2.8.0 | Apache-2.0 | https://github.com/googleapis/python-genai |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | https://greenlet.readthedocs.io |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | https://www.encode.io/httpcore/ |
| httpx | 0.28.1 | BSD | https://github.com/encode/httpx |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| jiter | 0.17.0 | MIT | https://github.com/pydantic/jiter/ |
| jsonschema | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
| openpyxl | 3.1.5 | MIT | https://foss.heptapod.net/openpyxl/openpyxl |
| playwright | 1.59.0 | Apache-2.0 | https://github.com/Microsoft/playwright-python |
| psycopg | 3.3.6 | LGPL-3.0-only | https://psycopg.org/ |
| psycopg-binary | 3.3.6 | LGPL-3.0-only | https://psycopg.org/ |
| pyasn1 | 0.6.4 | see project | https://github.com/pyasn1/pyasn1 |
| pyasn1_modules | 0.4.2 | BSD | https://github.com/pyasn1/pyasn1-modules |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pydantic | 2.13.5 | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | 2.46.5 | MIT | https://github.com/pydantic |
| pyee | 13.0.1 | MIT | https://github.com/jfhbrook/pyee |
| PyYAML | 6.0.3 | MIT | https://github.com/yaml/pyyaml |
| referencing | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing |
| requests | 2.34.2 | Apache-2.0 | https://github.com/psf/requests |
| rpds-py | 2026.6.3 | MIT | https://github.com/crate-py/rpds |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | https://github.com/python-trio/sniffio |
| tenacity | 9.1.4 | Apache-2.0 | https://github.com/jd/tenacity |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3 |
| websockets | 16.1.1 | BSD-3-Clause | https://github.com/python-websockets/websockets |
| xlsxwriter | 3.2.9 | BSD | https://github.com/jmcnamara/XlsxWriter |

## Not installed by ditl_sbp

Codex, Claude Code, Poppler, pandoc and Chromium are not installed by `install.sh`. If you install them yourself, they are governed by their own licenses and terms.
