#!/bin/bash
# collect-run-data.sh
#
# Pulls Prometheus metrics (CPU, memory, replica count) and Kubernetes
# scaling events for a specific run window, saving everything under
# results/<RUN_ID>/. Run this immediately after a k6 scenario completes,
# while kubectl events are still recent (Kubernetes only retains events for
# a limited time, typically ~1 hour by default).
#
# Usage: ./collect-run-data.sh <RUN_ID> <START_UNIX_TS> <END_UNIX_TS> [RESULTS_DIR]
# Example: ./collect-run-data.sh 20260721T090000Z_hpa_A_r1 1753088400 1753088640 results-long

set -e

RUN_ID=$1
START_TS=$2
END_TS=$3
RESULTS_DIR=${4:-results}
NAMESPACE=phpa-experiment
PROM_URL="http://localhost:9090"
STEP="5s"   # Prometheus scrape/query resolution - matches monitoring interval

if [[ -z "$RUN_ID" || -z "$START_TS" || -z "$END_TS" ]]; then
  echo "Usage: $0 <RUN_ID> <START_UNIX_TS> <END_UNIX_TS> [RESULTS_DIR]"
  exit 1
fi

OUTDIR="${RESULTS_DIR}/${RUN_ID}"
mkdir -p "$OUTDIR"

echo "=== Collecting data for run: $RUN_ID ==="
echo "Window: $START_TS -> $END_TS"

query_range() {
  local name=$1
  local query=$2
  echo "--- Querying: $name ---"
  curl -s -G "${PROM_URL}/api/v1/query_range" \
    --data-urlencode "query=${query}" \
    --data-urlencode "start=${START_TS}" \
    --data-urlencode "end=${END_TS}" \
    --data-urlencode "step=${STEP}" \
    > "${OUTDIR}/${name}.json"
}

# CPU usage per go-service pod (cores)
query_range "cpu_usage" \
  'rate(container_cpu_usage_seconds_total{namespace="'"$NAMESPACE"'",pod=~"go-service-.*"}[1m])'

# Memory usage per go-service pod (bytes)
query_range "memory_usage" \
  'container_memory_working_set_bytes{namespace="'"$NAMESPACE"'",pod=~"go-service-.*"}'

# Replica count over time (works for both HPA and PHPA, since both scale the
# same underlying Deployment)
query_range "replica_count" \
  'kube_deployment_status_replicas{namespace="'"$NAMESPACE"'",deployment="go-service"}'

# CPU utilization percentage as HPA/PHPA itself calculates it (relative to
# the pod CPU request) - useful for directly comparing against the 60% target
query_range "cpu_utilization_pct" \
  '100 * sum(rate(container_cpu_usage_seconds_total{namespace="'"$NAMESPACE"'",pod=~"go-service-.*"}[1m])) / sum(kube_pod_container_resource_requests{namespace="'"$NAMESPACE"'",pod=~"go-service-.*",resource="cpu"})'

echo "--- Capturing pod creation/readiness timestamps ---"
kubectl get pods -n "$NAMESPACE" -l app=go-service -o json \
  | jq '[.items[] | {name: .metadata.name, creationTimestamp: .metadata.creationTimestamp, readyCondition: (.status.conditions[] | select(.type=="Ready"))}]' \
  > "${OUTDIR}/pod_timestamps.json"

echo "--- Capturing recent scaling-related events ---"
kubectl get events -n "$NAMESPACE" --sort-by='.lastTimestamp' \
  --field-selector involvedObject.name=go-service \
  -o json > "${OUTDIR}/deployment_events.json"

echo "--- Capturing HPA or PHPA status/history at time of collection ---"
kubectl get hpa go-service-hpa -n "$NAMESPACE" -o json > "${OUTDIR}/hpa_status.json" 2>/dev/null || true
kubectl get phpa go-service-phpa -n "$NAMESPACE" -o json > "${OUTDIR}/phpa_status.json" 2>/dev/null || true

echo "=== Collection complete. Files saved under ${OUTDIR}/ ==="
ls -la "$OUTDIR"

# #!/bin/bash
# # collect-run-data.sh
# #
# # Pulls Prometheus metrics (CPU, memory, replica count) and Kubernetes
# # scaling events for a specific run window, saving everything under
# # results/<RUN_ID>/. Run this immediately after a k6 scenario completes,
# # while kubectl events are still recent (Kubernetes only retains events for
# # a limited time, typically ~1 hour by default).
# #
# # Usage: ./collect-run-data.sh <RUN_ID> <START_UNIX_TS> <END_UNIX_TS>
# # Example: ./collect-run-data.sh 20260721T090000Z_hpa_A_r1 1753088400 1753088640

# set -e

# RUN_ID=$1
# START_TS=$2
# END_TS=$3
# NAMESPACE=phpa-experiment
# PROM_URL="http://localhost:9090"
# STEP="5s"   # Prometheus scrape/query resolution - matches monitoring interval

# if [[ -z "$RUN_ID" || -z "$START_TS" || -z "$END_TS" ]]; then
#   echo "Usage: $0 <RUN_ID> <START_UNIX_TS> <END_UNIX_TS>"
#   exit 1
# fi

# OUTDIR="results/${RUN_ID}"
# mkdir -p "$OUTDIR"

# echo "=== Collecting data for run: $RUN_ID ==="
# echo "Window: $START_TS -> $END_TS"

# query_range() {
#   local name=$1
#   local query=$2
#   echo "--- Querying: $name ---"
#   curl -s -G "${PROM_URL}/api/v1/query_range" \
#     --data-urlencode "query=${query}" \
#     --data-urlencode "start=${START_TS}" \
#     --data-urlencode "end=${END_TS}" \
#     --data-urlencode "step=${STEP}" \
#     > "${OUTDIR}/${name}.json"
# }

# # CPU usage per go-service pod (cores)
# query_range "cpu_usage" \
#   'rate(container_cpu_usage_seconds_total{namespace="'"$NAMESPACE"'",pod=~"go-service-.*"}[1m])'

# # Memory usage per go-service pod (bytes)
# query_range "memory_usage" \
#   'container_memory_working_set_bytes{namespace="'"$NAMESPACE"'",pod=~"go-service-.*"}'

# # Replica count over time (works for both HPA and PHPA, since both scale the
# # same underlying Deployment)
# query_range "replica_count" \
#   'kube_deployment_status_replicas{namespace="'"$NAMESPACE"'",deployment="go-service"}'

# # CPU utilization percentage as HPA/PHPA itself calculates it (relative to
# # the pod CPU request) - useful for directly comparing against the 60% target
# query_range "cpu_utilization_pct" \
#   '100 * sum(rate(container_cpu_usage_seconds_total{namespace="'"$NAMESPACE"'",pod=~"go-service-.*"}[1m])) / sum(kube_pod_container_resource_requests{namespace="'"$NAMESPACE"'",pod=~"go-service-.*",resource="cpu"})'

# echo "--- Capturing pod creation/readiness timestamps ---"
# kubectl get pods -n "$NAMESPACE" -l app=go-service -o json \
#   | jq '[.items[] | {name: .metadata.name, creationTimestamp: .metadata.creationTimestamp, readyCondition: (.status.conditions[] | select(.type=="Ready"))}]' \
#   > "${OUTDIR}/pod_timestamps.json"

# echo "--- Capturing recent scaling-related events ---"
# kubectl get events -n "$NAMESPACE" --sort-by='.lastTimestamp' \
#   --field-selector involvedObject.name=go-service \
#   -o json > "${OUTDIR}/deployment_events.json"

# echo "--- Capturing HPA or PHPA status/history at time of collection ---"
# kubectl get hpa go-service-hpa -n "$NAMESPACE" -o json > "${OUTDIR}/hpa_status.json" 2>/dev/null || true
# kubectl get phpa go-service-phpa -n "$NAMESPACE" -o json > "${OUTDIR}/phpa_status.json" 2>/dev/null || true

# echo "=== Collection complete. Files saved under ${OUTDIR}/ ==="
# ls -la "$OUTDIR"