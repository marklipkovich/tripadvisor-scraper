# First, specify the base Docker image.
# You can see the Docker images from Apify at https://hub.docker.com/r/apify/.
# You can also use any other image from Docker Hub.
FROM apify/actor-python-playwright:3.14-1.58.0

# Store Camoufox Firefox binary in a fixed location accessible to all users.
ENV XDG_DATA_HOME=/opt/camoufox-data

# Copy requirements and install as root (camoufox fetch needs write access)
COPY --chown=root:root requirements.txt ./
USER root
RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && echo "Installing dependencies:" \
 && pip install -r requirements.txt \
 && echo "Fetching Camoufox Firefox binary:" \
 && python -m camoufox fetch \
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
