#!/bin/bash
# reset-between-runs.sh
#
# Restores the cluster to a clean baseline before a measured run.
# Usage: ./reset-between-runs.sh <hpa|phpa>
#
# Handles the two autoscalers differently where necessary:
# - HPA is stateless between evaluations, so removing/reapplying it and
#   waiting for baseline CPU is sufficient.
# - PHPA maintains a persistent replica-count history (ConfigMap) that
#   directly feeds its prediction model. If not cleared, a new run would be
#   contaminated by the previous run's tail-end replica counts, violating
#   repetition independence (see Chapter 3, Section 3.6.4).

set -e

AUTOSCALER=$1
NAMESPACE=phpa-experiment

if [[ "$AUTOSCALER" != "hpa" && "$AUTOSCALER" != "phpa" ]]; then
  echo "Usage: $0 <hpa|phpa>"
  exit 1
fi

echo "=== Resetting cluster for a $AUTOSCALER run ==="

echo "--- Cleaning up any leftover load-test pods ---"
kubectl delete pod k6-test k6-calibrate --ignore-not-found=true -n "$NAMESPACE"

echo "--- Removing both autoscaler objects (idempotent - ignore-not-found) ---"
kubectl delete hpa go-service-hpa --ignore-not-found=true -n "$NAMESPACE"
kubectl delete phpa go-service-phpa --ignore-not-found=true -n "$NAMESPACE"

echo "--- Clearing PHPA's replica-history ConfigMap (if it exists) ---"
# Always run this, regardless of which autoscaler is being set up next, so a
# stale history never leaks into a future PHPA run.
kubectl delete configmap predictive-horizontal-pod-autoscaler-go-service-phpa-data \
  --ignore-not-found=true -n "$NAMESPACE"

echo "--- Restoring go-service to minReplicas (1) ---"
kubectl scale deployment go-service --replicas=1 -n "$NAMESPACE"
kubectl rollout status deployment/go-service -n "$NAMESPACE" --timeout=120s

echo "--- Waiting for CPU/traffic to settle to baseline (60s) ---"
sleep 60

echo "--- Confirming all go-service pods are healthy ---"
kubectl get pods -l app=go-service -n "$NAMESPACE"
kubectl wait --for=condition=ready pod -l app=go-service -n "$NAMESPACE" --timeout=60s

echo "--- Applying $AUTOSCALER ---"
if [[ "$AUTOSCALER" == "hpa" ]]; then
  kubectl apply -f go-service-hpa.yaml
else
  kubectl apply -f go-service-phpa.yaml
  echo "--- Allowing PHPA one sync period to initialise cleanly (15s) ---"
  sleep 15
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_${AUTOSCALER}"
echo "=== Reset complete. Run ID: $RUN_ID ==="
echo "$RUN_ID" > /tmp/last_run_id.txt