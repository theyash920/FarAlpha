# Flask and MongoDB on Minikube

**Yash Chopra · Faralpha DevOps assignment**

Two Flask replicas serve a timestamp and a shared JSON collection. MongoDB runs as a single authenticated StatefulSet backed by a retained 2 GiB volume. A CPU-based Horizontal Pod Autoscaler can grow Flask to five replicas.

## Deploy and verify

Start Docker Desktop on macOS, or Docker Engine on Linux. Install Minikube, kubectl and Python 3.11 or later. On macOS, Homebrew supplies the Kubernetes tools with `brew install minikube kubectl`. Give Docker at least four CPUs and enough memory for a 4 GiB Minikube node. The first run downloads images and Python packages. [Minikube installation](https://minikube.sigs.k8s.io/docs/start/)

From the extracted project directory, run both commands in order.

```bash
cd ~/Downloads/faralpha
bash scripts/deploy.sh
python3 scripts/verify.py
```

The deployment script starts the single-node `faralpha` profile, preserves your current kubectl context, enables Metrics Server and builds the Flask image directly in Minikube. It creates the namespace and one Secret, applies the database and Flask manifests, then waits for both workloads and the metrics API. A source-content tag triggers a rollout when the application changes. Subsequent deployments reuse the existing database credentials. [Minikube image builds](https://minikube.sigs.k8s.io/docs/handbook/pushing/)

The verifier opens its own temporary port-forward, writes a uniquely marked document, replaces the MongoDB pod, and runs a three-minute load Job. Allow several additional minutes for metrics warm-up, database restart and scale-down. It prints the path to its evidence and returns a nonzero exit status on failure.

## Try the API

Run this in one terminal and leave it open. Port 8080 is bound to your loopback interface.

```bash
kubectl --context faralpha -n faralpha port-forward --address 127.0.0.1 svc/flask 8080:80
```

In a second terminal, run these requests. The first returns the welcome message with a current UTC timestamp. The POST returns HTTP 201 and `{"status":"Data inserted"}`. The final GET returns an array containing the submitted object. Each call to POST creates a record.

```bash
curl -i http://127.0.0.1:8080/
curl -i -H 'Content-Type: application/json' \
  --data '{"sampleKey":"sampleValue"}' http://127.0.0.1:8080/data
curl -i http://127.0.0.1:8080/data
```

| Request | Behavior |
| --- | --- |
| `GET /` | HTTP 200, plain text beginning with `Welcome to the Flask app! The current time is: `, followed by an ISO 8601 timestamp. |
| `POST /data` | Accepts a JSON object up to 1 MiB. Malformed JSON or an array returns 400. An unsupported media type returns 415. |
| `GET /data` | HTTP 200 and all documents from `flask_db.data`, with MongoDB's internal `_id` excluded. An empty collection yields `[]`. |
| `GET /livez` | Checks the Flask process. |
| `GET /readyz` | Reads the collection using the application's credentials. A database outage returns 503 and Kubernetes removes the pod from ready Service endpoints. |

## How the pods connect

The Flask Service has a cluster IP and routes port 80 to port 5000 on ready pods matching `app=flask`. A workstation port-forward selects one pod for the session. The load Job connects to the Service inside the cluster, so new connections can reach different ready replicas. [Kubernetes port forwarding](https://kubernetes.io/docs/tasks/access-application-cluster/port-forward-access-application-cluster/)

Flask connects to `mongodb://mongodb:27017/`. Kubelet supplies the pod's DNS resolver and namespace search suffixes. CoreDNS resolves the short name `mongodb` in the `faralpha` namespace. Its full name in this profile is `mongodb.faralpha.svc.cluster.local`. This Service is headless, so the DNS answer contains the ready database pod's IP. The StatefulSet uses the Service as its governing domain, giving its member the stable name `mongodb-0.mongodb.faralpha.svc.cluster.local`. A replacement may receive a new IP. The Flask deployment keeps the Service name in its connection settings. MongoDB has no host port, NodePort, LoadBalancer or Ingress. [Kubernetes Service and pod DNS](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/)

On an empty data directory, the official MongoDB entrypoint creates a bootstrap administrator in `admin`. It then runs the mounted initialization script to create `flask_app` with `readWrite` on `flask_db`. Flask receives only the two application fields from the Secret. PyMongo takes the credentials as separate parameters with `authSource=flask_db`, which also handles punctuation in passwords. Bootstrap runs on the first initialization of the volume. [Mongo image initialization](https://hub.docker.com/_/mongo)

## Resource requests and autoscaling

| Container | CPU request | CPU limit | Memory request | Memory limit |
| --- | ---: | ---: | ---: | ---: |
| Flask, per pod | 200m | 500m | 250M | 500M |
| MongoDB | 200m | 500m | 250M | 500M |

The scheduler uses requests to decide whether a node can fit a pod. CPU requests also influence the share of CPU under contention. The kernel can throttle either container at 0.5 CPU. Memory usage can rise above the 250 MB request. If the container hits its 500 MB limit under pressure, it can be OOM-killed. The memory quantities are decimal, as specified in the assignment. MongoDB's WiredTiger cache is set to 0.26 GB to leave room for other allocations. [Kubernetes resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) · [WiredTiger cache](https://www.mongodb.com/docs/manual/core/wiredtiger/)

The HPA compares average Flask pod CPU use with the **200m request**. A 70% target corresponds to **140m per pod**. For example, two eligible pods averaging 210m each are at 105% utilization. The basic calculation asks for `ceil(2 × 105 / 70) = 3` replicas. Metrics Server supplies the readings. Sampling, controller tolerance and pod readiness affect the timing. The configured range is **2–5**, with a 60-second scale-down stabilization window. [HPA algorithm and metrics](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)

## Design choices

| Decision | Rationale and alternative considered |
| --- | --- |
| Flask Deployment plus Gunicorn | All replicas share MongoDB, so Kubernetes can replace or scale them independently. Each pod uses one Gunicorn worker with four threads under its half-core limit. The Flask development server is used only for the optional host exercise. |
| ClusterIP plus a loopback port-forward | Works with the Docker driver on Apple Silicon and Linux. NodePort would also expose Flask through a node address and would require driver-specific host access. |
| One MongoDB StatefulSet plus a headless Service | Stable member identity and an explicit data claim make replacement easy to verify. A replica set would add failover, extra database members and additional storage beyond this single-node exercise. Replacing this singleton briefly interrupts reads and writes. |
| Static PV and PVC | The claim binds by name to a dedicated `ReadWriteOnce` PV, mounted at `/data/db`. Its backing path is `/data/faralpha/mongodb` inside the Minikube node. A dynamically provisioned claim would simplify a multi-node deployment. The static pair makes the assignment's binding and retention behavior inspectable. |
| Separate liveness and readiness checks | MongoDB downtime takes Flask out of service while its process stays alive. Restarting all Flask workers during an outage would add connection churn. |
| HTTP load against the welcome endpoint | This isolates Flask CPU demand from database I/O and collection growth. Separate tests exercise real writes, authenticated reads and recovery. |

The PV uses `Retain`. A MongoDB pod replacement preserves the claim and bytes. Stopping and restarting the same Minikube profile preserves its node disk. Deleting the profile removes that disk. [Minikube persistent volumes](https://minikube.sigs.k8s.io/docs/handbook/persistent_volumes/)

## Tests and results

The preparation run passed **19 offline tests** covering 10 manifest checks, 3 database initialization cases with a recording database object, and 6 verifier cases with synthetic metrics and HTTP counts. Python, Bash and JavaScript syntax checks passed. Docker, Minikube, kubectl and MongoDB were unavailable, and the package index could not supply the Flask dependencies here. The live Flask, MongoDB and HPA run is pending. [Recorded test output](evidence/workspace-checks.txt)

The live verifier runs the Flask unit suite inside the built image, then performs the following checks on the `faralpha` cluster.

| Scenario | What must be observed |
| --- | --- |
| API and shared state | Local GET returns a timestamp, POST returns 201 and GET returns the marker without `_id`. Each ready Flask pod independently returns the same marker. Invalid requests return the expected 400 or 415. |
| DNS and authentication | Both database DNS names resolve to the actual MongoDB pod address. The application account reads the marker. An anonymous read receives MongoDB code 13, an incorrect password receives 18, and an administrator-only operation under the app account receives 13. |
| Persistence | After replacing `mongodb-0`, its pod UID changes. Both PV and PVC UIDs stay the same and the original marker remains readable. |
| Scale out | Start from two Ready replicas and valid CPU metrics. Fortio targets 1,200 requests/second for 180 seconds with 32 concurrent clients and fresh connections. Record the actual rate, response codes, CPU percentage and Ready replica count. The verifier requires CPU above 70%, at least three Ready replicas, desired replicas within 2–5 and successful responses. |
| Scale in | After traffic stops, observe idle CPU and two desired and Ready replicas within the observation window. |

A live run writes `summary.json`, `hpa.csv`, `fortio.json`, the raw Fortio log, Flask unit output and port-forward logs under `evidence/run-…/`. The summary contains the measured peak CPU, actual QPS, response counts and observed replica peak. On failure it captures the error and available cluster diagnostics. After a successful run, zip the project folder again to include its measured results. [Fortio load options](https://github.com/fortio/fortio)

To watch the cluster while the verifier runs, open another terminal.

```bash
kubectl --context faralpha -n faralpha get hpa/flask --watch
```

## Build and push the image

The default Minikube path builds on the node. To deploy through Docker Hub, create a repository named `faralpha-flask` in your account, substitute your account name below, then run the following commands. A public repository is directly pullable by the cluster.

```bash
export DOCKERHUB_USER='your-docker-hub-username'
export APP_IMAGE="docker.io/$DOCKERHUB_USER/faralpha-flask:1.0.0"
docker login
docker build -t "$APP_IMAGE" .
docker push "$APP_IMAGE"
APP_IMAGE="$APP_IMAGE" bash scripts/deploy.sh
```

A native build matches the machine that built it. For a registry image usable on both an M-series Mac and an x86-64 reviewer machine, use this build-and-push step. Create the named builder once. Use a fresh tag for each subsequent release so cached images stay identifiable.

```bash
docker buildx create --name faralpha-multi --driver docker-container
docker buildx build --builder faralpha-multi --platform linux/arm64,linux/amd64 \
  -t "$APP_IMAGE" --push .
```

## Optional local Python setup

Part 1 can run independently of Kubernetes. Start with a fresh local MongoDB container and use the same shell for the following commands. A virtual environment isolates the project's Python packages, so installing or upgrading them leaves other projects' environments intact. The deployment uses Python 3.12 and pinned Flask, Gunicorn and PyMongo versions. The API contract follows the assignment's starter.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export MONGO_INITDB_ROOT_USERNAME=bootstrap_admin MONGO_APP_USERNAME=flask_app
export MONGO_INITDB_ROOT_PASSWORD="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export MONGO_APP_PASSWORD="$(python -c 'import secrets; print(secrets.token_hex(32))')"
docker run -d --name faralpha-local-db -p 127.0.0.1:27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME -e MONGO_INITDB_ROOT_PASSWORD -e MONGO_APP_USERNAME -e MONGO_APP_PASSWORD \
  --mount "type=bind,source=$PWD/k8s/database/init-app-user.js,target=/docker-entrypoint-initdb.d/10-app-user.js,readonly" \
  mongo:8.0.38
export MONGODB_URI='mongodb://127.0.0.1:27017/'
python -m flask --app app:create_app run --host 127.0.0.1 --port 5000
```

Use the same curl examples on port 5000. If macOS already occupies that port, choose 5001 in the Flask command and curl requests. In the initialized virtual environment, `python -m unittest discover -s tests -p 'test_app.py' -v` runs the Flask test client with a mocked MongoDB driver.

## Operations and troubleshooting

| Symptom | Check |
| --- | --- |
| HPA CPU reads `<unknown>` | Run `kubectl --context faralpha -n faralpha top pods` and `kubectl --context faralpha get apiservice v1beta1.metrics.k8s.io`. Verify Metrics Server is available and allow initial samples to arrive. |
| MongoDB starts but Flask stays unready | Inspect `kubectl --context faralpha -n faralpha logs mongodb-0` and `kubectl --context faralpha -n faralpha logs deploy/flask`. A retained data directory expects its original database credentials. Restore the matching Secret if it was removed. |
| Claim remains Pending or Released | Run `kubectl --context faralpha describe pv faralpha-mongo-pv` and `kubectl --context faralpha -n faralpha describe pvc mongo-data`. Check the existing binding, claim UID and empty storage class before changing storage. |
| Image pull fails | Describe the affected pod. For a registry image, check the tag, node architecture and pull access. For a local image, inspect `minikube -p faralpha image ls` and rerun the default deployment with `APP_IMAGE` unset. |
| Scale-out test fails | Inspect the recorded `hpa.csv`, Fortio response counts, pod scheduling events and `kubectl --context faralpha -n faralpha describe hpa flask`. These distinguish low achieved traffic, missing metrics and unschedulable replicas. |

To pause the cluster while retaining data, run `minikube stop -p faralpha`. Resume with `bash scripts/deploy.sh`. For a deliberate full reset of this assignment, `minikube delete -p faralpha` destroys the dedicated profile and its database.

## Project files

| Location | Contents |
| --- | --- |
| `app.py`, `requirements.txt`, `Dockerfile` | Flask API, Python dependency pins and container entry point. |
| `k8s/` | Namespace, Flask Deployment and Service, and CPU HPA. |
| `k8s/database/` | StatefulSet, internal Service, PV, PVC, and the Kustomize-generated initialization ConfigMap. Run `kubectl kustomize k8s/database` to inspect the rendered database YAML. |
| `bootstrap/mongodb-secret.template.yaml` | Secret manifest template. The deployment script substitutes freshly generated passwords in memory on first use. |
| `scripts/`, `tests/`, `evidence/` | Deployment, verification, load Job, test cases and recorded results. |
