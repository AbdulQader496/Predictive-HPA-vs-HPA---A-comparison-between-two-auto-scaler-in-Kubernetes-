# HPA vs PHPA Autoscaling Experiment

This repository extends the [URL shortener microservices demo](README-APP.md) into an experiment that compares two Kubernetes autoscalers on the same workload:

- **HPA**: the standard, reactive Horizontal Pod Autoscaler.
- **PHPA**: the [Predictive Horizontal Pod Autoscaler](https://github.com/jthomperoo/predictive-horizontal-pod-autoscaler), which uses a linear-regression model over recent replica history.

The measured workload is only the **Go redirect service**. It runs on a single-node Minikube cluster and is loaded with [k6](https://k6.io). Prometheus collects metrics, and a Python script turns each run into CSV summaries.

For the original application (Go, Python, Node and Redis services, the dashboard and the API), see [README-APP.md](README-APP.md).

---

## Project structure

```
URLshortner-microservices/
├── README.md                     This file
├── README-APP.md                 Original app documentation (upstream)
├── docker-compose.yml            Runs the full app locally (not used by the experiment)
├── sonar-project.properties      SonarCloud config
├── .github/workflows/
│   ├── deploy.yml                Builds and pushes Docker images on push to main
│   └── sonar.yml                 SonarCloud scan
│
├── go-service/                   ★ The workload under test
│   ├── main.go                   Gin app: /api/shorten, /:code redirect, /health
│   ├── Dockerfile                Multi-stage build (CGO enabled for SQLite)
│   └── go.mod / go.sum
├── python-service/               Dashboard and analytics (demo only, not measured)
├── node-service/                 URL metadata fetcher (demo only, not measured)
│
├── k8s/                          ★ Cluster manifests, run scripts, analysis
│   ├── go-service-deployment.yaml    go-service and Redis Deployments and Services
│   ├── go-service-hpa.yaml           HPA definition
│   ├── go-service-phpa.yaml          PHPA definition
│   ├── services-deployment.yaml      node-service and python-service (demo only)
│   ├── reset-between-runs.sh         Returns the cluster to a clean baseline, then applies HPA or PHPA
│   ├── watch-pods-during-run.sh      Snapshots pods every 3s during a run
│   ├── collect-run-data.sh           Pulls Prometheus metrics and k8s events for a run window
│   ├── analyze_results.py            Builds summary.csv, comparison.csv and pct_diff.csv
│   └── results*/                     Raw run data (not in git; see below)
│
├── k6/                           Short load scenarios (~3–4 min each)
│   ├── pilot-script.js               Constant 20 VUs for 90s, used for calibration
│   ├── scenario-a-steady.js          A: steady at 5 VUs
│   ├── scenario-b-spike.js           B: spike from 5 to 30 VUs
│   └── scenario-c-fluctuating.js     C: 5 → 15 → 30 → 5 → 30 → 5 VUs
└── k6-long/                      Long variants of A, B and C (~5–7 min each)
```

### Results data (not in git)

The `k8s/results*/` folders are listed in `.gitignore` and kept only on the experiment VM (about 51 MB). They are:

| Folder | Contents |
|---|---|
| `results/` | Primary set, 21 Jul (3 repetitions × 3 scenarios × 2 autoscalers) |
| `results-long/` | Long-duration set, 16 Aug (1 repetition each) |
| `results-extended-c/` | Extended Scenario C, 17 and 26 Aug |
| `results-extended-c-0831/` | Extended Scenario C, HPA rerun on 31 Aug |

### Changes made to go-service for the experiment

| Feature | Env var | Default in code | Value in deployment | Purpose |
|---|---|---|---|---|
| `burnCPU()` | `CPU_LOAD_ITERATIONS` | `20000` | `3000` | SHA-256 rounds per redirect, so the endpoint is CPU-bound and the autoscalers have a signal to react to |
| `seedDatabase()` | `SEED_COUNT` | `1000` | `1000` | Preloads `loadtest0001`…`loadtest1000` identically in every pod, so load tests never hit the write path |
| `/health` | none | none | none | Readiness and liveness probe that pings the database |
| Short URL base | `PUBLIC_BASE_URL` | `http://localhost:8000` | `http://10.251.70.226:8000` | Host used in the returned short URLs |
| Redis address | `REDIS_URL` | `localhost:6380` | `redis:6379` | Redis cache and pub/sub |

Pods deliberately have **no shared volume**. Each pod seeds its own SQLite file, which avoids write contention between replicas.

### Autoscaler settings

HPA and PHPA are configured the same way wherever both support a setting:

| Setting | Value |
|---|---|
| Target | 60% average CPU utilisation, relative to the 100m CPU request |
| Replicas | 1 to 5 |
| Scale up | No stabilisation window, up to +100% every 15s |
| Scale down | 300s stabilisation window, up to −50% every 60s |
| PHPA only | Linear model, `historySize: 6`, `lookAhead: 15s`, `syncPeriod: 15s`, `decisionType: maximum` |

Pod resources are requests of 100m CPU and 64Mi memory, with limits of 300m CPU and 128Mi memory.

---

## Prerequisites

The versions below are what the VM uses:

| Tool | Version | Notes |
|---|---|---|
| Ubuntu | 26.04 | VM, 3 CPU and 5 GB RAM given to Minikube |
| Docker | 29.x | Minikube driver and image builds |
| Minikube | v1.38 | |
| kubectl | v1.36 | |
| Helm | v4.3 | For Prometheus and the PHPA operator |
| k6 | any recent version | Run where it can reach go-service (see step 5) |
| Python 3 | 3.10 or newer | For `analyze_results.py` (standard library only) |
| jq, curl | any | Used by `collect-run-data.sh` |

---

## One-time setup

### 1. Start Minikube and metrics-server

```bash
minikube start --driver=docker --cpus=3 --memory=5000mb
minikube addons enable metrics-server
kubectl top nodes          # confirm metrics are coming through
```

### 2. Install monitoring (Prometheus and Grafana)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace
```

`collect-run-data.sh` expects Prometheus at `http://localhost:9090`. Forward it with:

```bash
kubectl port-forward --address 0.0.0.0 -n monitoring \
  svc/monitoring-kube-prometheus-prometheus 9090:9090
```

On the VM this port-forward runs as the systemd unit `prometheus-forward.service`. Similar units exist for go-service (`go-service-forward.service`, port 8000), Grafana (port 3000) and python-service (port 5000).

Get the Grafana admin password with:

```bash
kubectl get secret -n monitoring monitoring-grafana \
  -o jsonpath="{.data.admin-password}" | base64 --decode; echo
```

### 3. Install the PHPA operator

```bash
VERSION=<release tag>   # see https://github.com/jthomperoo/predictive-horizontal-pod-autoscaler/releases
helm install phpa-operator \
  https://github.com/jthomperoo/predictive-horizontal-pod-autoscaler/releases/download/${VERSION}/predictive-horizontal-pod-autoscaler-${VERSION}.tgz
kubectl get pods -l name=predictive-horizontal-pod-autoscaler
```

### 4. Build the image and deploy go-service

Images are built locally and loaded into Minikube; they are never pulled from a registry (`imagePullPolicy: Never`).

```bash
kubectl create namespace phpa-experiment
kubectl config set-context --current --namespace=phpa-experiment

cd go-service
docker build -t go-service:test .
minikube image load go-service:test
cd ..

kubectl apply -f k8s/go-service-deployment.yaml
kubectl run curl-test --image=curlimages/curl -it --rm --restart=Never \
  -- curl http://go-service:8000/health      # expect {"status":"healthy",...}
```

If the VM's IP changes, update `PUBLIC_BASE_URL`:

```bash
kubectl set env deployment/go-service PUBLIC_BASE_URL=http://<vm-ip>:8000
```

The demo services (`k8s/services-deployment.yaml`) are optional. **Scale them to zero or delete them before measured runs** so they don't use cluster headroom.

---

## Running a measured run

Every run follows the same five steps. Run the scripts from the `k8s/` folder, because they reference the YAML files by relative path.

### Run IDs

Name runs `<YYYYMMDD>_<scenario>_<autoscaler>_r<n>`, with an optional tag before `_r<n>`:

```
20260721_A_hpa_r1          primary set
20260816_B_phpa_long_r1    long set
20260817_C_ext_hpa_r1      extended Scenario C
```

`analyze_results.py` works out the scenario (`A`/`B`/`C`), autoscaler (`hpa`/`phpa`) and repetition from this name, so keep to the pattern.

### Step by step

```bash
cd k8s
AUTOSCALER=hpa               # hpa or phpa
RUN_ID=20260901_B_${AUTOSCALER}_r1
RESULTS=results-new          # results folder for this batch
DURATION=340                 # scenario length plus about 30s of buffer

# 1. Reset to baseline and apply the chosen autoscaler (see the table below)
./reset-between-runs.sh $AUTOSCALER

# 2. Start the pod watcher in the background
./watch-pods-during-run.sh $RUN_ID $DURATION $RESULTS &

# 3. Run k6 and record the start and end timestamps
START=$(date +%s)
k6 run -e BASE_URL=http://<vm-ip>:8000 \
  --summary-export=scenario-b-${AUTOSCALER}-r1.json ../k6/scenario-b-spike.js
END=$(date +%s)

# 4. Collect metrics immediately (Kubernetes only keeps events for about 1 hour)
./collect-run-data.sh $RUN_ID $START $END $RESULTS

# 5. Copy the k6 summary into the run folder
cp scenario-b-${AUTOSCALER}-r1.json $RESULTS/$RUN_ID/
```

For the matching PHPA run, set `AUTOSCALER=phpa` and repeat the same five steps.

### What the reset script does for HPA and PHPA

`./reset-between-runs.sh hpa` and `./reset-between-runs.sh phpa` share the same baseline steps. PHPA gets one extra wait at the end:

| Step | `hpa` | `phpa` |
|---|---|---|
| Delete leftover `k6-test` / `k6-calibrate` pods | ✓ | ✓ |
| Delete **both** autoscaler objects (`go-service-hpa`, `go-service-phpa`) | ✓ | ✓ |
| Delete PHPA's replica-history ConfigMap (`predictive-horizontal-pod-autoscaler-go-service-phpa-data`) | ✓ | ✓ |
| Scale go-service to 1 replica and wait for the rollout | ✓ | ✓ |
| Wait 60s for CPU to settle, then check pods are Ready | ✓ | ✓ |
| Apply the autoscaler | `go-service-hpa.yaml` | `go-service-phpa.yaml` |
| Wait one PHPA sync period (15s) so it starts cleanly | none | ✓ |

The ConfigMap is cleared on **every** reset, including HPA resets. PHPA's linear model predicts from this stored replica history, so a leftover history would carry the previous run's replica counts into the next PHPA run and break independence between repetitions.

After the reset, confirm the right autoscaler is active:

```bash
kubectl get hpa,phpa -n phpa-experiment     # only one of the two should be listed
```

**Important:**

- The k6 summary file must be named `scenario-*.json` or `k6_summary.json`, and it must be the **only** such file in the run folder. The analysis script reads the first match it finds.
- Alternate HPA and PHPA runs, and always run `reset-between-runs.sh` between them. Otherwise PHPA's replica history leaks from one run into the next.
- If k6 runs on a different machine from the VM, take `START` and `END` on the VM (`date +%s`), because Prometheus timestamps use the VM clock.

### Running k6 inside the cluster (alternative)

The calibration runs used a k6 pod inside the cluster, with the script mounted from a ConfigMap:

```bash
kubectl create configmap k6-scenario-a --from-file=scenario-a.js=../k6/scenario-a-steady.js
kubectl run k6-test --image=grafana/k6 --restart=Never --overrides='{
  "spec": {
    "containers": [{
      "name": "k6-test", "image": "grafana/k6",
      "command": ["k6", "run", "/scripts/scenario-a.js"],
      "volumeMounts": [{"name": "script", "mountPath": "/scripts"}]
    }],
    "volumes": [{"name": "script", "configMap": {"name": "k6-scenario-a"}}]
  }
}'
kubectl logs -f k6-test
```

The default `BASE_URL` in the scripts is `http://go-service:8000`, so no `-e` flag is needed in the cluster. The summary only appears in the pod logs, though. For runs you plan to analyse, use `--summary-export` from outside the cluster as shown above.

### Calibrating the CPU load

To change how much CPU each redirect costs:

```bash
kubectl set env deployment/go-service CPU_LOAD_ITERATIONS=3000
watch -n 2 kubectl top pod -l app=go-service
```

The aim is for Scenario A (5 VUs) to stay **below** the 60% target while Scenarios B and C (30 VUs) push above it.

---

## Analysing results

```bash
cd k8s
python3 analyze_results.py results-new
```

The script prints three tables and writes three files into the same folder:

| File | Contents |
|---|---|
| `summary.csv` | One row per run |
| `comparison.csv` | Mean, median and standard deviation per scenario and autoscaler |
| `pct_diff.csv` | PHPA vs HPA % difference per scenario, as `(phpa − hpa) / hpa × 100` |

### Metrics

| Metric | Source | How it is calculated |
|---|---|---|
| `mean_cpu_utilization_pct` | Prometheus | Mean of total CPU usage ÷ total CPU requested, over the run |
| `p95_latency_ms`, `throughput_rps`, `error_rate_pct` | k6 summary | Taken directly from k6 |
| `scale_up_events` | k8s events | Count of "Scaled up" events within the run window |
| `mean_scaling_delay_s` | Events and pod snapshots | Time from a scale-up event to the first **new** pod becoming Ready |
| `replica_minutes` | Prometheus | Sum of replicas × sampling interval |
| `cpu_waste_pct` | Prometheus | (requested − used CPU) ÷ requested CPU |
| `estimated_cost_usd` | Derived | From replica-minutes at an illustrative $0.024 per core-hour and $0.003 per GB-hour. **Only useful for comparison, not a real bill.** |

### Files in each run folder

```
<RUN_ID>/
├── cpu_usage.json                    Prometheus: CPU cores per pod
├── cpu_utilization_pct.json          Prometheus: % of CPU request
├── memory_usage.json                 Prometheus: working-set bytes per pod
├── replica_count.json                Prometheus: Deployment replicas over time
├── deployment_events.json            kubectl events for go-service
├── pod_timestamps.json               Pod snapshot taken after the run
├── pod_snapshots_continuous.jsonl    Snapshots every 3s during the run (Aug runs onwards)
├── hpa_status.json / phpa_status.json   Autoscaler state; the unused one is empty
└── scenario-*.json                   k6 summary
```

---

## Scenarios at a glance

| Scenario | Short (`k6/`) | Long (`k6-long/`) | What it tests |
|---|---|---|---|
| **A: Steady** | 30s ramp, 3m at 5 VUs, 30s ramp down | 6m hold | Normal operation, over-provisioning |
| **B: Spike** | 30s at 5, jump to 30 in 10s, hold 2m, back to 5 | 4m hold | Reaction speed, scaling delay |
| **C: Fluctuating** | 30s stages: 5 → 15 → 30 → 5 → 30 → 5 | 60s stages | How stable the predictions are under repeated swings |

Every scenario requests random seeded codes with `redirects: 0`, so k6 measures go-service's own 301 response rather than the external redirect target.

---

## Known caveats

- **Scaling-delay figures in `results/` (21 Jul) are not reliable.** Those runs were collected before `watch-pods-during-run.sh` existed, so they fall back to a single snapshot taken after the run. Several HPA delays exceed the run length (500–1800s). Treat `mean_scaling_delay_s` from that set with caution. The August sets use continuous snapshots and filter events to the run window.
- **The long and extended sets have one repetition each,** so their standard deviations are not meaningful.
- **The extended Scenario C runs were not paired on the same day** (PHPA on 17 Aug, HPA on 26 Aug). `results-extended-c-0831/` holds a later HPA rerun that is not yet in any summary.
- **`CPU_LOAD_ITERATIONS` differs by location:** the code default is 20000, the deployment YAML sets 3000, and the k6 comments mention 1500. Before quoting it, confirm the running value with:
  ```bash
  kubectl get deployment go-service -o jsonpath='{.spec.template.spec.containers[0].env}'
  ```
- `PUBLIC_BASE_URL` is hard-coded to the VM's IP. Update it if the network changes.
