#!/usr/bin/env bash

set -Eeo pipefail

pre_run () {
  zenml integration install evidently
  zenml integration install sklearn
}

pre_run_forced () {
  zenml integration install evidently -y
  zenml integration install sklearn -y
}
