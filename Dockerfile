# Archie agent container.
# Contains system tools for development work. Agent and shared source are mounted at
# runtime for fast iteration — only dependencies are baked into the image.

FROM debian:trixie-slim

# Build args for matching host user identity (avoids file permission issues)
ARG USERNAME=archie
ARG USER_UID=1000
ARG TARGETARCH

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# --- System packages ---
# Core utilities and developer tools available in Debian repos.
# fd-find is packaged as "fd-find" in Debian but the binary is "fdfind".
# Create a symlink so "fd" works as expected.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    chromium \
    openssh-client \
    gnupg \
    curl \
    dnsutils \
    procps \
    fd-find \
    git \
    iputils-ping \
    mariadb-client \
    nano \
    netcat-openbsd \
    pandoc \
    poppler-utils \
    postgresql-client \
    redis-tools \
    ripgrep \
    shellcheck \
    sqlite3 \
    sudo \
    tree \
    unzip \
    wget \
    zip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd

# --- uv (Python package manager + Python installer) ---
# Install Python into a shared, world-readable location (NOT root's home cache).
# uv sync below must use the SAME dir so the venv symlinks resolve for the
# non-root runtime user. Install the version pinned by .python-version.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python
COPY .python-version /opt/archie/.python-version
RUN uv python install "$(cat /opt/archie/.python-version)" && \
    PYTHON_PATH=$(find /opt/python -name "cpython-*" -type d | head -1) && \
    ln -s "$PYTHON_PATH/bin/python3" /usr/local/bin/python3 && \
    ln -s /usr/local/bin/python3 /usr/local/bin/python

# --- Node.js (latest LTS; includes npm and npx) + agent-browser + Cloudflare cf CLI ---
# cf is pinned: it is beta, and the cloudflare plugin's skill names exact commands.
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install --global agent-browser cf@1.0.0-beta.21 && \
    node --version && npx --version && agent-browser --version && CF_SEND_TELEMETRY=false cf --version && \
    rm -rf /var/lib/apt/lists/*

# Install jq
RUN curl -L "https://github.com/jqlang/jq/releases/latest/download/jq-linux-${TARGETARCH}" \
        -o /usr/local/bin/jq && chmod +x /usr/local/bin/jq

# --- Go (latest 1.27.x from go.dev; Debian ships an older release) ---
# Pinned to the host's minor version. The archive is verified against the
# SHA-256 published in the go.dev release index.
ARG GO_MINOR=1.27
RUN set -eux; \
    GO_RELEASE=$(curl -fsSL "https://go.dev/dl/?mode=json&include=all" \
        | jq -r --arg m "go${GO_MINOR}." --arg a "${TARGETARCH}" \
            '[.[] | select(.stable and (.version | startswith($m)))][0] | .files[] | select(.os == "linux" and .arch == $a and .kind == "archive") | "\(.filename) \(.sha256)"'); \
    GO_FILE=${GO_RELEASE% *}; GO_SHA=${GO_RELEASE#* }; \
    test -n "${GO_FILE}" && test "${#GO_SHA}" -eq 64; \
    curl -fsSL "https://go.dev/dl/${GO_FILE}" -o /tmp/go.tar.gz; \
    echo "${GO_SHA}  /tmp/go.tar.gz" | sha256sum -c -; \
    tar -C /usr/local -xzf /tmp/go.tar.gz; \
    rm /tmp/go.tar.gz; \
    /usr/local/go/bin/go version
ENV PATH="/usr/local/go/bin:${PATH}"

# --- yq (YAML processor, like jq for YAML) ---
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${TARGETARCH}" \
    -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq

# --- AWS CLI v2 ---
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip; \
    else \
        curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscli.zip; \
    fi && \
    unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip

# --- OpenTofu (Terraform-compatible IaC tool) ---
RUN curl -fsSL https://get.opentofu.org/install-opentofu.sh | bash -s -- --install-method standalone

# --- terraform-docs (generates docs from Terraform modules) ---
RUN TFDOCS_VERSION=$(curl -fsSL https://api.github.com/repos/terraform-docs/terraform-docs/releases/latest | jq -r .tag_name) && \
    curl -fsSL "https://github.com/terraform-docs/terraform-docs/releases/download/${TFDOCS_VERSION}/terraform-docs-${TFDOCS_VERSION}-linux-${TARGETARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin terraform-docs && chmod +x /usr/local/bin/terraform-docs

# --- Scalr CLI ---
# Credentials come from SCALR_TOKEN / SCALR_HOSTNAME / SCALR_ACCOUNT (plugins/scalr).
RUN SCALR_VERSION=$(curl -fsSL https://api.github.com/repos/Scalr/scalr-cli/releases/latest | jq -r .tag_name | sed 's/^v//') && \
    SCALR_BASE="https://github.com/Scalr/scalr-cli/releases/download/v${SCALR_VERSION}" && \
    SCALR_ZIP="scalr-cli_${SCALR_VERSION}_linux_${TARGETARCH}.zip" && \
    cd /tmp && \
    curl -fsSLO "${SCALR_BASE}/${SCALR_ZIP}" && \
    curl -fsSLO "${SCALR_BASE}/scalr-cli_${SCALR_VERSION}_SHA256SUMS" && \
    grep " ${SCALR_ZIP}\$" "scalr-cli_${SCALR_VERSION}_SHA256SUMS" | sha256sum -c - && \
    unzip -q "${SCALR_ZIP}" scalr -d /usr/local/bin && \
    chmod +x /usr/local/bin/scalr && \
    rm -f /tmp/scalr-cli_* && \
    scalr -version

# --- GitHub CLI ---
# The real binary is diverted to /usr/bin/gh-base so package upgrades keep it there.
# /usr/local/bin/gh (copied below) is a wrapper that picks the GitHub token per call.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && rm -rf /var/lib/apt/lists/* && \
    dpkg-divert --local --rename --divert /usr/bin/gh-base --add /usr/bin/gh && \
    /usr/bin/gh-base --version

# --- difftastic (structural diff tool) ---
RUN DIFFT_VERSION=$(curl -s https://api.github.com/repos/Wilfred/difftastic/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    case ${TARGETARCH} in \
        amd64) DIFFT_ARCH="x86_64" ;; \
        arm64) DIFFT_ARCH="aarch64" ;; \
    esac && \
    curl -fsSL "https://github.com/Wilfred/difftastic/releases/download/${DIFFT_VERSION}/difft-${DIFFT_VERSION}-${DIFFT_ARCH}-unknown-linux-gnu.tar.gz" \
        -o /tmp/difft.tar.gz && \
    tar -xzf /tmp/difft.tar.gz -C /usr/local/bin && \
    chmod +x /usr/local/bin/difft && \
    rm /tmp/difft.tar.gz

# --- xh (modern HTTP client, like httpie but faster) ---
RUN XH_VERSION=$(curl -s https://api.github.com/repos/ducaale/xh/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    case ${TARGETARCH} in \
        amd64) XH_ARCH="x86_64" ;; \
        arm64) XH_ARCH="aarch64" ;; \
    esac && \
    curl -L "https://github.com/ducaale/xh/releases/download/${XH_VERSION}/xh-${XH_VERSION}-${XH_ARCH}-unknown-linux-musl.tar.gz" \
        -o /tmp/xh.tar.gz && \
    tar -xzf /tmp/xh.tar.gz -C /tmp && \
    mv /tmp/xh-${XH_VERSION}-${XH_ARCH}-unknown-linux-musl/xh /usr/local/bin/ && \
    chmod +x /usr/local/bin/xh && \
    rm -rf /tmp/xh*

# --- just (command runner, like make but simpler) ---
RUN JUST_VERSION=$(curl -s https://api.github.com/repos/casey/just/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    case ${TARGETARCH} in \
        amd64) JUST_ARCH="x86_64" ;; \
        arm64) JUST_ARCH="aarch64" ;; \
    esac && \
    curl -L "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-${JUST_ARCH}-unknown-linux-musl.tar.gz" \
        -o /tmp/just.tar.gz && \
    tar -xzf /tmp/just.tar.gz -C /usr/local/bin just && \
    chmod +x /usr/local/bin/just && \
    rm /tmp/just.tar.gz

# --- shfmt (shell script formatter) ---
RUN SHFMT_VERSION=$(curl -s https://api.github.com/repos/mvdan/sh/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    case ${TARGETARCH} in \
        amd64) SHFMT_ARCH="amd64" ;; \
        arm64) SHFMT_ARCH="arm64" ;; \
    esac && \
    curl -L "https://github.com/mvdan/sh/releases/download/${SHFMT_VERSION}/shfmt_${SHFMT_VERSION}_linux_${SHFMT_ARCH}" \
        -o /usr/local/bin/shfmt && \
    chmod +x /usr/local/bin/shfmt

# --- Pre-seed GitHub SSH host keys (avoids interactive prompts on first git clone) ---
RUN mkdir -p /etc/ssh && ssh-keyscan github.com >> /etc/ssh/ssh_known_hosts 2>/dev/null

# --- Create non-root user matching host UID ---
# Group is created with the username (no host GID needed).
# Only UID needs to match for file permission alignment.
RUN groupadd ${USERNAME} \
    && useradd --uid ${USER_UID} --gid ${USERNAME} -m -s /bin/bash ${USERNAME} \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# --- Pre-install agent dependencies (cached layer) ---
# The agent depends on archie-shared (a workspace sibling). To resolve deps correctly,
# uv needs a workspace root pyproject.toml, the shared package source, and the agent's
# pyproject.toml. Shared source is baked in so the editable install resolves at build
# time, but the host shared/ is mounted over it at runtime (like agent) for fast
# iteration — no rebuild needed for shared changes.
#
# We create a container-specific workspace manifest that only includes agent and shared
# (cli is host-only and not present in the container). Without this, uv fails to parse
# the workspace when the cli/ directory is missing.
# .python-version was already copied above. UV_PYTHON_INSTALL_DIR (/opt/python) is
# inherited from the ENV set earlier, so the venv reuses the shared Python install.
COPY shared/ /opt/archie/shared/
COPY agent/pyproject.toml /opt/archie/agent/
RUN cat > /opt/archie/pyproject.toml <<'EOF'
[project]
name = "archie-nexus"
version = "0.1.0"
requires-python = ">=3.13,<3.14"

[tool.uv.workspace]
members = ["agent", "shared"]

[tool.uv.sources]
archie-agent = { workspace = true }
archie-shared = { workspace = true }
EOF
ENV UV_PROJECT_ENVIRONMENT=/opt/archie/venv
RUN cd /opt/archie && uv sync --package archie-agent --no-dev --no-install-project
ENV PATH="/opt/archie/venv/bin:$PATH"

# --- Entrypoint script and gh wrapper ---
COPY entrypoint.sh /opt/archie/entrypoint.sh
COPY container/bin/gh /usr/local/bin/gh
RUN chmod +x /opt/archie/entrypoint.sh /usr/local/bin/gh

# --- Create workspace mount point ---
RUN mkdir /workspace

# --- Hand ownership to the runtime user ---
# The venv and entrypoint were created as root; the container runs as $USERNAME.
# entrypoint.sh runs uv sync which may update the venv, so it needs write access.
# Pre-create Kiro's state directory so a file bind mount of data.sqlite3 does
# not cause Docker to create the parent directory as root at container start.
RUN mkdir -p /home/${USERNAME}/.local/share/kiro-cli \
    && chown -R ${USERNAME}:${USERNAME} /opt/archie /workspace /home/${USERNAME}/.local

WORKDIR /workspace

EXPOSE 8080

USER ${USERNAME}

# Use Debian's native Chromium build (Chrome for Testing has no Linux ARM64 build).
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium

# Turn off the cf CLI's usage telemetry; every ephemeral container would look like a new install.
ENV CF_SEND_TELEMETRY=false

# Go module/build caches live in the user's home (GOPATH defaults to ~/go).
ENV PATH="/home/${USERNAME}/go/bin:${PATH}"

# Install the latest stable Kiro CLI non-interactively.
# This mirrors the Linux path from https://cli.kiro.dev/install: select the
# architecture archive for Debian's glibc environment, verify it against the
# release manifest, extract it, and run its setup with setup prompts disabled.
RUN set -eux; \
    mkdir -p "/home/${USERNAME}/.kiro"; \
    case "${TARGETARCH}" in \
        amd64) KIRO_ARCH="x86_64" ;; \
        arm64) KIRO_ARCH="aarch64" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    KIRO_FILENAME="kirocli-${KIRO_ARCH}-linux.zip"; \
    KIRO_BASE_URL="https://prod.download.cli.kiro.dev/stable/latest"; \
    KIRO_MANIFEST=$(curl --proto '=https' --tlsv1.2 -fsSL "${KIRO_BASE_URL}/manifest.json"); \
    KIRO_SHA256=$(echo "${KIRO_MANIFEST}" | jq -r --arg filename "${KIRO_FILENAME}" \
        '.packages[] | select(.download | endswith($filename)) | .sha256' | head -1); \
    test -n "${KIRO_SHA256}" && test "${#KIRO_SHA256}" -eq 64; \
    KIRO_TMP=$(mktemp -d); \
    trap 'rm -rf "${KIRO_TMP}"' EXIT; \
    curl --proto '=https' --tlsv1.2 -fsSL "${KIRO_BASE_URL}/${KIRO_FILENAME}" \
        -o "${KIRO_TMP}/${KIRO_FILENAME}"; \
    echo "${KIRO_SHA256}  ${KIRO_TMP}/${KIRO_FILENAME}" | sha256sum -c -; \
    unzip -q "${KIRO_TMP}/${KIRO_FILENAME}" -d "${KIRO_TMP}/extract"; \
    chmod +x "${KIRO_TMP}/extract/kirocli/install.sh"; \
    rm -f "/home/${USERNAME}/.local/bin/kiro-cli" "/home/${USERNAME}/.local/bin/kiro-cli-chat"; \
    KIRO_CLI_SKIP_SETUP=1 "${KIRO_TMP}/extract/kirocli/install.sh"; \
    chmod +x "/home/${USERNAME}/.local/bin/kiro-cli" "/home/${USERNAME}/.local/bin/kiro-cli-chat"; \
    chown -R "${USERNAME}:${USERNAME}" "/home/${USERNAME}/.kiro" "/home/${USERNAME}/.local"

# kiro-cli installs to ~/.local/bin; ensure it's on PATH for non-login exec
# (e.g. `docker run archie:latest kiro-cli ...`), not just interactive shells.
ENV PATH="/home/${USERNAME}/.local/bin:${PATH}"

CMD ["/opt/archie/entrypoint.sh"]