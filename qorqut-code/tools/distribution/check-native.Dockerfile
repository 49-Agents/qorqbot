# Test-only host prerequisites, never included in the Qorqbot download.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3.12 python3.12-venv git tmux ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
