# Following Apify's official camoufox pattern (github.com/apify/actor-camoufox-scraper):
# - Use actor-python-playwright (NOT the camoufox variant)
# - Install camoufox via pip, then run `python -m camoufox fetch` as myuser
# - No USER root / XDG_CACHE_HOME / chmod needed: the base image already
#   runs as myuser, so the binary lands in /home/myuser/.cache/camoufox/
#   which myuser can always read at runtime.
FROM apify/actor-python-playwright:3.12

# Copy requirements first for better layer caching.
COPY --chown=myuser:myuser requirements.txt ./
RUN echo "Python version:" \
 && python --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

# Fetch the Camoufox Firefox binary as myuser.
# Binary is stored in /home/myuser/.cache/camoufox/ — readable at runtime.
RUN python -m camoufox fetch

COPY --chown=myuser:myuser . ./
RUN python3 -m compileall -q src/

CMD ["python3", "-m", "src"]
