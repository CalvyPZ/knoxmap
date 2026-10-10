#!/bin/bash
# Copy the repo mounted at /knoxmap into /tmp/build, without host build output.
stage_tree() {
  rm -rf /tmp/build
  mkdir -p /tmp/build /knoxmap/releases
  tar -C /knoxmap -cf - \
    --exclude=.git \
    --exclude=.venv \
    --exclude=node_modules \
    --exclude=pybuild \
    --exclude=release \
    --exclude=releases \
    --exclude=released \
    --exclude=output \
    --exclude=cache \
    --exclude=logs \
    --exclude=vendor \
    --exclude=KnoxMap/worlded/src \
    --exclude=KnoxMap/worlded/build \
    --exclude=__pycache__ \
    --exclude=Helpers/target \
    --exclude=compiler/target \
    . | tar -C /tmp/build -xf -
}
