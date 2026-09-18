# The browser suite's runner: Chromium, its fonts and its system libraries
# as Microsoft ships them, so a screenshot or a contrast ratio measured
# here is the one CI measures.
#
# The tag must match the playwright pin in requirements-dev.lock. The image
# carries the browser build for exactly that version; a mismatch fails
# loudly on launch ("Executable doesn't exist"), not quietly.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /repo

# Dependencies first, from the lock and by hash, so an edit to the code
# reuses this layer instead of reinstalling the toolchain.
COPY requirements-dev.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock

COPY . .
RUN pip install --no-cache-dir --no-deps -e .

# The suite's own fixture seeds a catalog and starts the app, so there is
# no second service. Anything after `run --rm browser` is appended here.
ENTRYPOINT ["pytest", "-m", "browser", "--tracing=retain-on-failure", "--screenshot=only-on-failure"]
CMD ["-q"]
