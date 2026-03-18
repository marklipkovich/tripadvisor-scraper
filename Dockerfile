FROM apify/actor-python-playwright:3.14-1.58.0

# Camoufox stores its Firefox binary in XDG_CACHE_HOME (not XDG_DATA_HOME).
# Set a fixed global path so the binary baked in during build is found at runtime.
ENV XDG_CACHE_HOME=/opt/camoufox-cache

# ── Camoufox binary layer (713 MB) ───────────────────────────────────────────
# Installed BEFORE copying requirements.txt so this layer is only invalidated
# when CAMOUFOX_VER changes — not when other packages or source code change.
# Update CAMOUFOX_VER here whenever you bump camoufox in requirements.txt.
USER root
ARG CAMOUFOX_VER=0.4.11
RUN pip install --no-cache-dir "camoufox>=${CAMOUFOX_VER}" \
 && python -m camoufox fetch \
 && chmod -R 755 /opt/camoufox-cache

# Copy requirements and install remaining dependencies (camoufox already satisfied above)
COPY --chown=root:root requirements.txt ./
RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

USER myuser

# Next, copy the remaining files and directories with the source code.
# Since we do this after installing the dependencies, quick build will be really fast
# for most source file changes.
COPY --chown=myuser:myuser . ./

# Use compileall to ensure the runnability of the Actor Python code.
RUN python -m compileall -q src/

# Specify how to launch the source code of your Actor.
CMD ["python", "-m", "src"]
