#!/bin/bash
# watch-pods-during-run.sh
#
# Fixes the scaling-delay collection bug: a single kubectl get pods snapshot
# taken AFTER a run misses any pod that was created and later deleted during
# that same run (e.g. scaled up, then scaled back down). This script instead
# takes a snapshot every 3 seconds for the run's duration, so every pod that
# ever existed during the run - even briefly - gets captured at least once
# with its readiness timestamp, before it can be deleted.
#
# Usage: ./watch-pods-during-run.sh <RUN_ID> <DURATION_SECONDS> [RESULTS_DIR]
# Run this in the background (with &) right before starting k6, matching the
# scenario's approximate total duration + ~30s buffer.
# RESULTS_DIR defaults to "results" if not given.

set -e

RUN_ID=$1
DURATION=$2
RESULTS_DIR=${3:-results}
NAMESPACE=phpa-experiment
OUTDIR="${RESULTS_DIR}/${RUN_ID}"
mkdir -p "$OUTDIR"
OUTFILE="${OUTDIR}/pod_snapshots_continuous.jsonl"

> "$OUTFILE"  # truncate/create fresh

echo "Watching pods for ${RUN_ID}, every 3s for ${DURATION}s..."

END=$((SECONDS + DURATION))
while [ $SECONDS -lt $END ]; do
  kubectl get pods -n "$NAMESPACE" -l app=go-service -o json >> "$OUTFILE"
  echo "---SNAPSHOT-BOUNDARY---" >> "$OUTFILE"
  sleep 3
done

echo "Done watching. Snapshots saved to ${OUTFILE}"