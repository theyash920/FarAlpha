#!/usr/bin/env python3
"""Exercise the dedicated faralpha profile and keep the measurements from this run."""
import csv
import json
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
KUBECTL = ["kubectl", "--context", "faralpha", "-n", "faralpha"]
HTTP = build_opener(ProxyHandler({}))


def k(*args, stdin=None, timeout=40):
    proc = subprocess.run(KUBECTL + list(args), input=stdin, text=True, capture_output=True,
                          timeout=timeout, cwd=ROOT)
    if proc.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def get(resource, *args):
    return json.loads(k("get", resource, *args, "-o", "json"))


def check(passed, message):
    if not passed:
        raise RuntimeError(message)


def until(description, probe, seconds=180, interval=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = probe()
        if result:
            return result
        time.sleep(interval)
    raise RuntimeError(f"Timed out after {seconds}s waiting for {description}.")


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def forwarded_service(logfile):
    with logfile.open("w") as log:
        proc = subprocess.Popen(KUBECTL + ["port-forward", "--address", "127.0.0.1",
                                "svc/flask", ":80"], stdout=log, stderr=subprocess.STDOUT)
        try:
            def endpoint():
                transcript = logfile.read_text()
                check(proc.poll() is None, f"Port-forward exited: {transcript.strip()}")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", transcript)
                return f"http://127.0.0.1:{match[1]}" if match else None

            yield until("the local Flask port-forward", endpoint, seconds=40, interval=0.25)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def request(base, route, body=None, media="application/json"):
    req = Request(base + route, data=body, headers={"Content-Type": media})
    try:
        response = HTTP.open(req, timeout=8)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


def api_roundtrip(url, document):
    code, text = request(url, "/")
    prefix = "Welcome to the Flask app! The current time is: "
    check(code == 200 and text.decode().startswith(prefix), "Unexpected welcome response.")
    stamp = datetime.fromisoformat(text.decode()[len(prefix):])
    check(stamp.tzinfo is not None, "The welcome timestamp has no UTC offset.")
    code, body = request(url, "/data", json.dumps(document).encode())
    check(code == 201 and json.loads(body) == {"status": "Data inserted"}, "POST /data failed.")
    code, body = request(url, "/data")
    rows = json.loads(body)
    check(code == 200 and document in rows and all("_id" not in row for row in rows),
          "GET /data did not return the inserted document with the required projection.")
    for payload, media, expected in ((b"[1]", "application/json", 400),
                                     (b"{", "application/json", 400),
                                     (b"text", "text/plain", 415)):
        check(request(url, "/data", payload, media)[0] == expected,
              f"Unexpected response to invalid input with content type {media}.")
    return {"welcome": 200, "timestamp": stamp.isoformat(), "insert": 201, "read": 200,
            "json_array": 400, "malformed_json": 400, "text_input": 415}


def ready(pod):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))


def persistence(out, document):
    claim = get("pvc/mongo-data")
    check(claim["status"]["phase"] == "Bound", "MongoDB claim is not Bound.")
    volume = claim["spec"]["volumeName"]
    before = get("pod/mongodb-0")
    check(any(o["kind"] == "StatefulSet" and o["name"] == "mongodb" and o.get("controller")
              for o in before["metadata"].get("ownerReferences", [])), "Unexpected MongoDB pod owner.")
    old_uid = before["metadata"]["uid"]
    pv_uid = get(f"pv/{volume}")["metadata"]["uid"]
    k("delete", "pod/mongodb-0", "--timeout=120s", timeout=140)

    def replacement():
        for pod in get("pods", "-l", "app=mongodb")["items"]:
            if pod["metadata"]["uid"] != old_uid and ready(pod):
                return pod["metadata"]["uid"]

    new_uid = until("a new Ready MongoDB pod", replacement, seconds=300)

    def row_recovered():
        try:
            status, data = request(url, "/data")
        except (URLError, TimeoutError, ConnectionError):
            return False
        return status == 200 and document in json.loads(data)

    k("rollout", "status", "deployment/flask", "--timeout=120s", timeout=135)
    with forwarded_service(out / "port-forward-after-restart.log") as url:
        until("the existing row to be readable after replacement", row_recovered, seconds=100)
    after = get("pvc/mongo-data")
    check(after["metadata"]["uid"] == claim["metadata"]["uid"] and after["status"]["phase"] == "Bound"
          and after["spec"]["volumeName"] == volume and get(f"pv/{volume}")["metadata"]["uid"] == pv_uid,
          "MongoDB volume or claim changed during pod replacement.")
    return {"old_pod_uid": old_uid, "new_pod_uid": new_uid, "pvc_uid": after["metadata"]["uid"],
            "pv_uid": pv_uid, "volume": volume, "marker_read_back": True}


def fortio_result(log):
    for match in re.finditer(r"(?m)^[ \t]*\{", log):
        try:
            data, _ = json.JSONDecoder().raw_decode(log[match.end() - 1:])
        except ValueError:
            continue
        if isinstance(data, dict) and "RetCodes" in data:
            return data
    raise RuntimeError("Fortio did not produce a JSON report. See fortio.log.")


def autoscale(out, summary):
    with (out / "hpa.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["utc", "phase", "cpu_percent_of_request", "desired", "ready"])
        samples = []

        def sample(phase):
            hpa = get("hpa/flask")
            spec, status = hpa["spec"], hpa.get("status", {})
            check((spec["minReplicas"], spec["maxReplicas"]) == (2, 5), "Unexpected HPA bounds.")
            check(any(m.get("resource", {}).get("name") == "cpu" and
                      m["resource"]["target"].get("averageUtilization") == 70 for m in spec["metrics"]),
                  "The deployed HPA has a different CPU target.")
            cpu = next((m["resource"].get("current", {}).get("averageUtilization") for m in
                        status.get("currentMetrics", []) if m.get("resource", {}).get("name") == "cpu"), None)
            desired = status.get("desiredReplicas")
            replicas = get("deployment/flask").get("status", {}).get("readyReplicas", 0)
            writer.writerow([utc(), phase, cpu, desired, replicas])
            stream.flush()
            if desired:
                check(2 <= desired <= 5, f"HPA desired count outside [2, 5]: {desired}")
            return cpu, desired, replicas

        def idle(phase):
            cpu, desired, replicas = sample(phase)
            return cpu is not None and cpu <= 70 and (desired, replicas) == (2, 2)

        until("two Ready replicas and a measured idle CPU baseline", lambda: idle("baseline"),
              seconds=300, interval=5)
        job = k("create", "-f", "tests/load-job.yaml", "-o", "name").strip()
        summary["load_job"] = job
        print(f"Started {job}. Sampling the HPA every 5 seconds.", flush=True)
        try:
            def finished():
                samples.append(sample("load"))
                conditions = get(job).get("status", {}).get("conditions", [])
                for condition in conditions:
                    if condition["status"] == "True" and condition["type"] == "Failed":
                        raise RuntimeError(f"Load Job failed: {condition.get('reason', 'unknown')}")
                return any(c["type"] == "Complete" and c["status"] == "True" for c in conditions)

            until("the three-minute load Job", finished, seconds=410, interval=5)
            log = k("logs", job)
            (out / "fortio.log").write_text(log)
            result = fortio_result(log)
            (out / "fortio.json").write_text(json.dumps(result, indent=2) + "\n")
            cpu_values = [cpu for cpu, _, _ in samples if cpu is not None]
            summary["load"] = {"http_status_counts": result["RetCodes"], "actual_qps": result["ActualQPS"],
                               "peak_cpu_percent": max(cpu_values, default=0),
                               "peak_ready": max(x[2] for x in samples),
                               "peak_desired": max((x[1] or 0) for x in samples)}
            print(f"Observed during load: {summary['load']}", flush=True)
            codes = result["RetCodes"]
            check(codes.get("200", 0) > 0 and sum(codes.values()) == codes["200"],
                  "The load run returned failed requests. See fortio.json.")
            check(summary["load"]["peak_cpu_percent"] > 70 and summary["load"]["peak_ready"] > 2,
                  "The run did not demonstrate both CPU above 70% and a third Ready replica.")
            until("the HPA to return to two Ready replicas", lambda: idle("cooldown"), seconds=360, interval=5)
            summary["cooldown_ready"] = 2
        finally:
            if not (out / "fortio.log").exists():
                try:
                    (out / "fortio.log").write_text(k("logs", job, "--pod-running-timeout=5s", timeout=10))
                except (RuntimeError, subprocess.TimeoutExpired):
                    pass
            k("delete", job, "--ignore-not-found", "--wait=false")


def main():
    marker = uuid.uuid4().hex
    out = ROOT / "evidence" / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + marker[:6])
    out.mkdir(parents=True)
    print(f"Evidence: {out}", flush=True)
    result = {"started": utc(), "marker": marker, "status": "failed"}
    try:
        result["kubernetes"] = json.loads(k("version", "-o", "json"))["serverVersion"]["gitVersion"]
        result["image"] = get("deployment/flask")["spec"]["template"]["spec"]["containers"][0]["image"]
        pods = [p for p in get("pods", "-l", "app=flask")["items"] if ready(p)]
        check(len(pods) >= 2, "Deploy first and wait for at least two Ready Flask pods.")
        print("Checking API responses, per-pod DNS and database permissions.", flush=True)
        tests = (ROOT / "tests/test_app.py").read_text()
        unit_output = k("exec", "-i", pods[0]["metadata"]["name"], "--", "python", "-", stdin=tests)
        (out / "flask-unit.txt").write_text(unit_output)
        result["flask_unit_tests"] = "passed"
        document = {"sampleKey": "sampleValue", "faralphaRun": marker}
        with forwarded_service(out / "port-forward.log") as url:
            result["http"] = api_roundtrip(url, document)
            mongo_ip = get("pod/mongodb-0")["status"]["podIP"]
            result["pod_checks"] = []
            for pod in pods:
                line = k("exec", "-i", pod["metadata"]["name"], "--", "python", "-", marker, mongo_ip,
                         stdin=(ROOT / "tests/pod_check.py").read_text()).strip()
                result["pod_checks"].append(line)
        print("Replacing mongodb-0 and checking the marker on its original claim.", flush=True)
        result["persistence"] = persistence(out, document)
        autoscale(out, result)
        result["status"] = "passed"
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        result["error"] = str(exc) or type(exc).__name__
        print(result["error"], file=sys.stderr)
        try:
            (out / "diagnostics.txt").write_text(k("get", "pods,pvc,hpa", "-o", "wide") + "\n" +
                                                 k("get", "events", "--field-selector", "type=Warning"))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
    finally:
        result["finished"] = utc()
        (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"{result['status'].upper()} | {out / 'summary.json'}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
