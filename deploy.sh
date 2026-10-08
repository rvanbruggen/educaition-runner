#!/bin/bash
# Deploy on the Docker host (same machine as positron-admin).
# Usage on the host: ~/educaition-runner/deploy.sh
set -e

cd ~/educaition-runner
echo "Pulling latest code..."
git pull

echo "Rebuilding and restarting..."
# `up --build` builds first and only then recreates, so the running
# container keeps serving during the build.
docker compose up -d --build --remove-orphans

echo "Done. Waiting for startup..."
sleep 3
docker compose logs --tail 5
