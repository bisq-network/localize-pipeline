FROM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36 AS gh-builder

ARG GH_VERSION=2.96.0
ARG GH_COMMIT=b300f2ec7ec9dc9addc39b2ad88c54097ded7ca0
ARG GRPC_VERSION=1.82.1
ARG X_TEXT_VERSION=0.40.0
ENV CGO_ENABLED=0

WORKDIR /src/gh
RUN git clone --depth 1 --branch "v${GH_VERSION}" \
        https://github.com/cli/cli.git . && \
    test "$(git rev-parse HEAD)" = "$GH_COMMIT" && \
    go get "google.golang.org/grpc@v${GRPC_VERSION}" && \
    go get "golang.org/x/text@v${X_TEXT_VERSION}" && \
    GH_VERSION="${GH_VERSION}" go run script/build.go bin/gh && \
    install -D bin/gh /out/gh
RUN go version -m /out/gh > /tmp/gh-buildinfo && \
    grep -E "google[.]golang[.]org/grpc[[:space:]]+v${GRPC_VERSION}([[:space:]]|$)" \
        /tmp/gh-buildinfo && \
    grep -E "golang[.]org/x/text[[:space:]]+v${X_TEXT_VERSION}([[:space:]]|$)" \
        /tmp/gh-buildinfo

FROM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36 AS yq-builder

ARG YQ_VERSION=4.53.3
ARG YQ_COMMIT=1b9b4ac5187171d2e5e3129be0cfa827c7f9d53d
ARG X_TEXT_VERSION=0.40.0
ENV CGO_ENABLED=0

WORKDIR /src/yq
RUN git clone --depth 1 --branch "v${YQ_VERSION}" \
        https://github.com/mikefarah/yq.git . && \
    test "$(git rev-parse HEAD)" = "$YQ_COMMIT" && \
    go get "golang.org/x/text@v${X_TEXT_VERSION}" && \
    go build -trimpath -ldflags="-s -w" -o /out/yq .
RUN /out/yq --version && \
    go version -m /out/yq > /tmp/yq-buildinfo && \
    grep -E "golang[.]org/x/text[[:space:]]+v${X_TEXT_VERSION}([[:space:]]|$)" \
        /tmp/yq-buildinfo

FROM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36 AS tx-builder

ARG TX_VERSION=1.6.17
ARG TX_COMMIT=30dac142446db7bd1919894e9eb93545f58cc980
ARG GO_GIT_VERSION=5.19.2
ENV CGO_ENABLED=0

WORKDIR /src/tx
RUN git clone --depth 1 --branch "v${TX_VERSION}" \
        https://github.com/transifex/cli.git . && \
    test "$(git rev-parse HEAD)" = "$TX_COMMIT" && \
    go get "github.com/go-git/go-git/v5@v${GO_GIT_VERSION}" && \
    go build -p=1 -trimpath \
        -ldflags="-s -w -X github.com/transifex/cli/internal/txlib.Version=v${TX_VERSION}" \
        -o /out/tx .
RUN /out/tx --version && \
    go version -m /out/tx > /tmp/tx-buildinfo && \
    grep -E "github[.]com/go-git/go-git/v5[[:space:]]+v${GO_GIT_VERSION}([[:space:]]|$)" \
        /tmp/tx-buildinfo

FROM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36 AS gosu-builder

ARG GOSU_VERSION=1.19
ARG GOSU_COMMIT=6456aaa0f3c854d199d0f037f068eb97515b7513
ARG X_SYS_VERSION=0.46.0
ENV CGO_ENABLED=0

WORKDIR /src/gosu
RUN git clone --depth 1 --branch "${GOSU_VERSION}" \
        https://github.com/tianon/gosu.git . && \
    test "$(git rev-parse HEAD)" = "$GOSU_COMMIT" && \
    go get "golang.org/x/sys@v${X_SYS_VERSION}" && \
    go build -trimpath -ldflags="-s -w" -o /out/gosu .
RUN /out/gosu --version && \
    go version -m /out/gosu > /tmp/gosu-buildinfo && \
    grep -E "golang[.]org/x/sys[[:space:]]+v${X_SYS_VERSION}([[:space:]]|$)" \
        /tmp/gosu-buildinfo

FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

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

COPY --from=yq-builder /out/yq /usr/bin/yq
RUN yq --version

COPY --from=tx-builder /out/tx /usr/local/bin/tx
RUN tx --version

COPY --from=gosu-builder /out/gosu /usr/sbin/gosu
RUN gosu --version

# Create a non-root user 'appuser' with a fixed, non-conflicting UID/GID.
# The home directory is created at /home/appuser.
RUN groupadd --gid 9999 appuser && \
    useradd --uid 9999 --gid 9999 --shell /bin/bash --create-home appuser

# Make python3.11 the default python3
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

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

# Default command is the orchestration script so bare docker run uses PR gating.
CMD ["/app/update-translations.sh"]
