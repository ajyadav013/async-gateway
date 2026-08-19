# Build the wheel, then prove it installs and passes its suite clean.
#
# The defect this image exists to catch is the one a development checkout
# structurally cannot show you: `asyncio_gateway` once imported a dependency
# it never declared, so every check passed in a tree that already had that
# package installed and the *published artifact* was unusable
# (`ModuleNotFoundError: ujson` on the first import). An editable install
# cannot see that -- it puts the source tree itself on `sys.path`, so the
# package metadata is never the thing being exercised.
#
# Hence two stages: the build stage produces the wheel and the sdist, and
# the runtime stage installs the wheel into a clean image and runs the
# suite against it.
#
# The source tree is still copied into the runtime stage, and that is not
# an oversight -- several suites read the repository *as data*.
# `tests/test_docs.py` parses `README.md`, `tests/test_packaging.py` parses
# `.github/workflows/ci.yml` and `LICENSE`, and both it and
# `tests/test_no_blocking_io.py` AST-scan every file under
# `asyncio_gateway/`. Omitting the tree would not isolate the wheel, it would
# just fail collection.
#
# So the isolation is *asserted* rather than inferred from a missing COPY:
# `docker/run-tests.sh` refuses to start unless `asyncio_gateway` imports out
# of `site-packages`. See the comment in that file for why the weaker
# arrangement is the one that fails silently.
#
# This is a library. The image is a test and reproducibility harness, not a
# deployable service -- there is no entry point to start and no port to
# serve. `docker run` runs the suite and exits.
#
# `PYTHON_VERSION` selects the interpreter, defaulting to the 3.12 the
# image has always used. `requires-python` is `>=3.10` and CI claims the
# whole 3.10-3.14 range, so the claim is only worth what has actually been
# run: building with `--build-arg PYTHON_VERSION=3.10` reproduces one leg
# of that matrix locally. Both stages take it, so the wheel is built and
# installed by the same interpreter.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- build --
FROM python:${PYTHON_VERSION}-slim AS build

WORKDIR /src

# `build` is deliberately not in `[project.optional-dependencies].dev` --
# see CLAUDE.md -- so it is installed here explicitly rather than arriving
# with the dev extra.
RUN python -m pip install --no-cache-dir --upgrade pip build

# Only what the PEP 517 build actually reads. Copying the whole tree here
# would put `.git`, the coverage database and every local artifact into the
# build context's layer for no benefit.
COPY pyproject.toml MANIFEST.in README.md LICENSE CHANGELOG.md ./
COPY asyncio_gateway/ ./asyncio_gateway/

RUN python -m build --wheel --sdist --outdir /dist

# -------------------------------------------------------------- runtime --
FROM python:${PYTHON_VERSION}-slim AS runtime

# No cached wheel may satisfy the install below: a cache hit would hide
# exactly the packaging defect this stage exists to catch.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=build /dist/ /dist/

# From the artifact, not from the tree, and with the `dev` extra resolved
# off the wheel's own metadata -- so the extra that CI and the container
# test against is the one the *published* distribution declares.
RUN python -m pip install --no-cache-dir "$(ls /dist/*.whl)[dev]"

# The suite, its configuration, and the repository files the suite reads
# as data (see the header).
COPY pyproject.toml .flake8 MANIFEST.in README.md LICENSE CHANGELOG.md ./
COPY .github/ ./.github/
COPY docs/ ./docs/
COPY examples/ ./examples/
COPY tests/ ./tests/
COPY docker/run-tests.sh /usr/local/bin/run-tests

# `asyncio_gateway/` is a SYMLINK to the installed package, not a copy of the
# checkout -- and the difference is the whole point of this image.
#
# The suites that need it on disk need it at exactly `REPO_ROOT/
# asyncio_gateway` (they compute it as `Path(__file__).parents[1]`), and
# pytest prepends the rootdir to `sys.path`, so a *copied* tree there would
# shadow `site-packages` and the suite would silently go back to testing
# the checkout. That is not hypothetical: it is what the first build of
# this image did, and `run-tests` caught it.
#
# Symlinked, the two are the same bytes by construction. The AST scans in
# `tests/test_no_blocking_io.py` and `tests/test_packaging.py` therefore
# read the source that actually shipped in the wheel, which is a stronger
# check than scanning the checkout, and `import asyncio_gateway` resolves to
# that same installed package however `sys.path` happens to be ordered.
RUN ln -s "$(python -c 'import sysconfig, pathlib; \
        print(pathlib.Path(sysconfig.get_paths()["purelib"]) / "asyncio_gateway")')" \
        /app/asyncio_gateway \
 && chmod +x /usr/local/bin/run-tests

# Non-root. The suite writes only to temporary directories, so it needs no
# ownership of anything it did not create.
RUN useradd --create-home --uid 10001 gateway \
 && chown -R gateway:gateway /app
USER gateway

# Fail loudly at build time if the wheel does not import out of a clean
# environment -- the original headline defect, asserted rather than hoped
# for. `--import-mode=importlib` keeps the rootdir off `sys.path`, so
# `import asyncio_gateway` cannot silently resolve to a stray directory.
RUN cd / && python -c "import asyncio_gateway; print(asyncio_gateway.__version__)"

ENTRYPOINT ["run-tests"]
CMD ["-q"]
