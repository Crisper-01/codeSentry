FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    clang \
    clang-tools \
    cmake \
    make \
    ninja-build \
    pkg-config \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Conteneur persistant pour docker compose exec / Docker SDK
CMD ["sleep", "infinity"]
