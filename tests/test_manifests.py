"""Offline checks for the assignment's cross-resource contracts. Requires PyYAML."""
import unittest
from pathlib import Path
from string import Template

import yaml

ROOT = Path(__file__).resolve().parents[1]


def docs(path):
    return list(yaml.safe_load_all((ROOT / path).read_text()))


def container(workload):
    return workload["spec"]["template"]["spec"]["containers"][0]


class ManifestContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web, = docs("k8s/flask-deployment.yaml")
        cls.web_service, = docs("k8s/flask-service.yaml")
        cls.db_service, cls.db = docs("k8s/database/mongodb.yaml")
        cls.pv, cls.claim = docs("k8s/database/storage.yaml")
        cls.hpa, = docs("k8s/hpa.yaml")
        cls.job, = docs("tests/load-job.yaml")
        cls.db_build, = docs("k8s/database/kustomization.yaml")
        template = Template((ROOT / "bootstrap/mongodb-secret.template.yaml").read_text())
        cls.secret = yaml.safe_load(template.substitute(ROOT_PASSWORD="test-root", APP_PASSWORD="test-app"))

    def test_namespace(self):
        namespace, = docs("k8s/namespace.yaml")
        for resource in (self.web, self.web_service, self.db_service, self.db, self.claim,
                         self.hpa, self.job, self.secret):
            self.assertEqual(resource["metadata"]["namespace"], namespace["metadata"]["name"])
        self.assertNotIn("namespace", self.pv["metadata"])
        self.assertEqual(self.db_build["namespace"], namespace["metadata"]["name"])

    def test_service_selectors_and_named_ports(self):
        for svc, owner in ((self.web_service, self.web), (self.db_service, self.db)):
            pod = owner["spec"]["template"]
            self.assertEqual(svc["spec"]["selector"], owner["spec"]["selector"]["matchLabels"])
            self.assertEqual(svc["spec"]["selector"], pod["metadata"]["labels"])
            self.assertEqual(svc["spec"]["ports"][0]["targetPort"], container(owner)["ports"][0]["name"])
            self.assertEqual(svc["spec"]["type"], "ClusterIP")
        self.assertEqual(self.web_service["spec"]["ports"][0]["port"], 80)
        self.assertEqual(container(self.web)["ports"][0]["containerPort"], 5000)

    def test_mongo_dns_and_auth_wiring(self):
        name = self.db_service["metadata"]["name"]
        self.assertEqual(self.db_service["spec"]["clusterIP"], "None")
        self.assertEqual(self.db["spec"]["serviceName"], name)
        env = {e["name"]: e for e in container(self.web)["env"]}
        port = self.db_service["spec"]["ports"][0]["port"]
        self.assertEqual(env["MONGODB_URI"]["value"], f"mongodb://{name}:{port}/")
        self.assertIn("--auth", container(self.db)["args"])
        self.assertEqual(self.db["spec"]["replicas"], 1)

    def test_secret_keys_and_root_isolation(self):
        for workload, keys in ((self.web, {"app-username", "app-password"}),
                               (self.db, set(self.secret["stringData"]))):
            refs = [e["valueFrom"]["secretKeyRef"] for e in container(workload)["env"] if "valueFrom" in e]
            self.assertEqual({r["name"] for r in refs}, {self.secret["metadata"]["name"]})
            self.assertEqual({r["key"] for r in refs}, keys)

    def test_database_init_configmap_and_mount(self):
        builder = self.db_build["configMapGenerator"][0]
        key, filename = builder["files"][0].split("=")
        spec = self.db["spec"]["template"]["spec"]
        volume = next(v for v in spec["volumes"] if "configMap" in v)
        mount = next(v for v in container(self.db)["volumeMounts"] if v["name"] == volume["name"])
        self.assertEqual((volume["configMap"]["name"], volume["configMap"]["items"][0]["key"]),
                         (builder["name"], key))
        self.assertEqual(mount["mountPath"], "/docker-entrypoint-initdb.d")
        self.assertTrue((ROOT / "k8s/database" / filename).is_file())
        self.assertEqual(self.db_build["resources"], ["storage.yaml", "mongodb.yaml"])

    def test_retained_pv_is_bound_to_the_claim_and_database_path(self):
        pv, pvc = self.pv["spec"], self.claim["spec"]
        self.assertEqual(pv["claimRef"], {"namespace": "faralpha", "name": self.claim["metadata"]["name"]})
        self.assertEqual(pvc["volumeName"], self.pv["metadata"]["name"])
        self.assertEqual(pv["storageClassName"], pvc["storageClassName"])
        self.assertEqual(pv["capacity"], pvc["resources"]["requests"])
        self.assertEqual(pv["accessModes"], pvc["accessModes"])
        self.assertEqual(pv["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(pv["hostPath"]["path"], "/data/faralpha/mongodb")
        volume = next(v for v in self.db["spec"]["template"]["spec"]["volumes"] if "persistentVolumeClaim" in v)
        self.assertEqual(volume["persistentVolumeClaim"]["claimName"], self.claim["metadata"]["name"])
        mount = next(m for m in container(self.db)["volumeMounts"] if m["name"] == volume["name"])
        self.assertEqual(mount["mountPath"], "/data/db")

    def test_cpu_and_decimal_memory_quantities(self):
        for workload in (self.db, self.web):
            self.assertEqual(container(workload)["resources"], {"requests": {"cpu": "200m", "memory": "250M"},
                                                               "limits": {"cpu": "500m", "memory": "500M"}})

    def test_hpa_range_and_target(self):
        spec = self.hpa["spec"]
        self.assertEqual(self.hpa["apiVersion"], "autoscaling/v2")
        self.assertEqual(spec["scaleTargetRef"], {"apiVersion": self.web["apiVersion"],
                         "kind": self.web["kind"], "name": self.web["metadata"]["name"]})
        self.assertEqual((self.web["spec"]["replicas"], spec["minReplicas"], spec["maxReplicas"]), (2, 2, 5))
        self.assertEqual(spec["metrics"], [{"type": "Resource", "resource": {"name": "cpu",
                         "target": {"type": "Utilization", "averageUtilization": 70}}}])

    def test_health_probes(self):
        web = container(self.web)
        for kind, path in (("startupProbe", "/livez"), ("livenessProbe", "/livez"), ("readinessProbe", "/readyz")):
            self.assertEqual(web[kind]["httpGet"], {"path": path, "port": web["ports"][0]["name"]})

    def test_load_is_bounded_and_internal(self):
        args = container(self.job)["args"]
        self.assertEqual(args[-1], f"http://{self.web_service['metadata']['name']}/")
        self.assertIn("-keepalive=false", args)
        self.assertEqual(args[args.index("-t") + 1], "180s")
        self.assertGreater(self.job["spec"]["activeDeadlineSeconds"], 180)
        self.assertEqual(self.job["spec"]["backoffLimit"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
