#!/bin/sh
# Assert the suite is testing the INSTALLED WHEEL, then run it.
#
# The point of running the suite in this image is that it exercises the
# built artifact rather than the source checkout. That property is not
# self-evident: the repository has to be present on disk anyway, because
# several suites read it as data -- `tests/test_docs.py` parses
# `README.md`, `tests/test_packaging.py` parses `.github/workflows/ci.yml`
# and `LICENSE`, and both it and `tests/test_no_blocking_io.py` AST-scan
# every file under `asyncio_gateway/`. A source tree that is present but
# must not be imported is exactly the situation where "we didn't copy it,
# so it can't be imported" stops being true, quietly.
#
# So the guarantee is asserted here instead of inferred from a missing
# COPY. If `asyncio_gateway` resolves anywhere but `site-packages`, this
# exits non-zero before a single test runs, and the failure names both
# paths rather than leaving a green suite that proved nothing.
set -eu

python - <<'PY'
import pathlib
import sysconfig

import asyncio_gateway

resolved = pathlib.Path(asyncio_gateway.__file__).resolve()
site_packages = pathlib.Path(sysconfig.get_paths()['purelib']).resolve()

if not resolved.is_relative_to(site_packages):
    raise SystemExit(
        'REFUSING TO RUN: asyncio_gateway resolved to the source tree, not '
        'the installed wheel.\n'
        f'  imported from: {resolved}\n'
        f'  expected under: {site_packages}\n'
        'The suite would pass against the checkout and prove nothing '
        'about the built artifact.')

print(f'asyncio_gateway {asyncio_gateway.__version__} from {resolved.parent}')
PY

# `pytest`, not `python -m pytest`: the module form prepends the working
# directory to `sys.path`, which would put the source tree ahead of
# `site-packages` and defeat the check above.
exec pytest "$@"
