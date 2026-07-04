FROM golang:1.26.4-bookworm@sha256:5d2b868674b57c9e48cdd39e891acce4196b6926ca6d11e9c270a8f85106203d AS gh-builder

ARG GH_VERSION=2.93.0
ENV CGO_ENABLED=0

RUN GOBIN=/out go install github.com/cli/cli/v2/cmd/gh@v${GH_VERSION}

FROM ubuntu:24.04

# Set SHELL to fail on pipeline errors
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies, add repositories, and clean up in a single layer.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        git \
        jq \
        python3.11 \
        python3.11-venv \
        python3-pip \
        openssh-client && \
    apt-get remove -y software-properties-common && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=gh-builder /out/gh /usr/bin/gh
RUN gh --version

# Download and install yq for YAML processing
RUN set -eux; \
    YQ_VERSION=v4.53.3; \
    YQ_ARCH="$(dpkg --print-architecture)"; \
    YQ_BINARY="yq_linux_${YQ_ARCH}"; \
    curl -sSL -o /usr/bin/yq "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/${YQ_BINARY}"; \
    if [ "$YQ_ARCH" = "amd64" ]; then \
        YQ_SHA256="fa52a4e758c63d38299163fbdd1edfb4c4963247918bf9c1c5d31d84789eded4"; \
    elif [ "$YQ_ARCH" = "arm64" ]; then \
        YQ_SHA256="578648e463a11c1b6db6010cbf41eafed6bee79466fcffa1bb446672cf7945ea"; \
    else \
        echo "Unsupported architecture for yq: $YQ_ARCH" >&2; \
        exit 1; \
    fi; \
    echo "$YQ_SHA256 /usr/bin/yq" | sha256sum -c -; \
    chmod +x /usr/bin/yq

# Download and install gosu for user switching
RUN set -eux; \
    GOSU_VERSION=1.19; \
    GOSU_ARCH="$(dpkg --print-architecture)"; \
    curl -sSL -o /usr/sbin/gosu "https://github.com/tianon/gosu/releases/download/$GOSU_VERSION/gosu-$GOSU_ARCH"; \
    if [ "$GOSU_ARCH" = "amd64" ]; then \
        GOSU_SHA256="52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0"; \
    elif [ "$GOSU_ARCH" = "arm64" ]; then \
        GOSU_SHA256="3a8ef022d82c0bc4a98bcb144e77da714c25fcfa64dccc57f6aba7ae47ff1a44"; \
    else \
        echo "Unsupported architecture: $GOSU_ARCH" >&2; \
        exit 1; \
    fi; \
    echo "$GOSU_SHA256 /usr/sbin/gosu" | sha256sum -c -; \
    chmod +x /usr/sbin/gosu

# Create a non-root user 'appuser' with a fixed, non-conflicting UID/GID.
# The home directory is created at /home/appuser.
RUN groupadd --gid 9999 appuser && \
    useradd --uid 9999 --gid 9999 --shell /bin/bash --create-home appuser

# Make python3.11 the default python3
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

# Install Transifex CLI
RUN TX_VERSION="v1.6.17" && \
    CONTAINER_ARCH=$(dpkg --print-architecture) && \
    if [ "$CONTAINER_ARCH" = "amd64" ]; then TX_ARCH="linux-amd64"; \
    elif [ "$CONTAINER_ARCH" = "arm64" ]; then TX_ARCH="linux-arm64"; \
    else echo "Unsupported architecture for Transifex CLI: $CONTAINER_ARCH"; exit 1; fi && \
    curl -Lf -O "https://github.com/transifex/cli/releases/download/${TX_VERSION}/tx-${TX_ARCH}.tar.gz" && \
    tar -xzf "tx-${TX_ARCH}.tar.gz" -C /usr/local/bin tx && \
    rm "tx-${TX_ARCH}.tar.gz" && \
    tx --version

# Private deploy and GPG keys are mounted as runtime secrets and installed by
# docker-entrypoint.sh so they never persist in image layers.
RUN mkdir -p /home/appuser/.ssh /home/appuser/.gnupg && \
    chmod 700 /home/appuser/.ssh /home/appuser/.gnupg && \
    chown -R appuser:appuser /home/appuser/.ssh /home/appuser/.gnupg

WORKDIR /app

# Copy source and install python dependencies
COPY requirements.txt .
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt
COPY . .

# Ensure the entrypoint and orchestration script are executable
RUN chmod +x /app/docker/docker-entrypoint.sh /app/update-translations.sh

# Set entrypoint
ENTRYPOINT ["/app/docker/docker-entrypoint.sh"]

# Bake the pinned SSH host keys into the image for enhanced security
COPY docker/ssh_known_hosts /etc/ssh/ssh_known_hosts
RUN chmod 644 /etc/ssh/ssh_known_hosts

# Default command is now the main python script (can be overridden in docker-compose.yml)
CMD ["python3", "-m", "localize.translate_localization_files"]
