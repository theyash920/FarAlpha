"""Check that recorded load evidence distinguishes a successful scale test from a false pass."""
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

module = importlib.util.spec_from_file_location("verify", Path(__file__).resolve().parents[1] / "scripts/verify.py")
verify = importlib.util.module_from_spec(module)
module.loader.exec_module(verify)


class EvidenceChecks(unittest.TestCase):
    def exercise(self, samples, codes):
        samples = iter(samples)
        latest = None
        job_polls = 0
        # Each tuple is (CPU percent, desired replicas, Ready replicas).
        def resource(name):
            nonlocal latest, job_polls
            if name == "hpa/flask":
                latest = next(samples)
                return {"spec": {"minReplicas": 2, "maxReplicas": 5, "metrics": [{"resource":
                        {"name": "cpu", "target": {"averageUtilization": 70}}}]}, "status": {
                        "currentMetrics": [{"resource": {"name": "cpu", "current":
                        {"averageUtilization": latest[0]}}}], "desiredReplicas": latest[1]}}
            if name == "deployment/flask":
                return {"status": {"readyReplicas": latest[2]}}
            self.assertEqual(name, "job.batch/test")
            job_polls += 1
            return {"status": {"conditions": [{"type": "Complete", "status": "True"}] if job_polls == 2 else []}}

        def command(*args, **kwargs):
            if args[0] == "create":
                return "job.batch/test"
            if args[0] == "logs":
                return 'Fortio output\n' + json.dumps({"RetCodes": codes, "ActualQPS": 12}) + '\n'
            self.assertEqual(args[0], "delete")
            return ""

        with tempfile.TemporaryDirectory() as directory, patch.object(verify, "get", resource), \
                patch.object(verify, "k", command), patch.object(verify.time, "sleep"), patch("builtins.print"):
            out, result = Path(directory), {}
            verify.autoscale(out, result)
            with (out / "hpa.csv").open() as stream:
                phases = [r["phase"] for r in csv.DictReader(stream)]
            self.assertEqual(phases, ["baseline", "load", "load", "cooldown"])
            self.assertEqual(json.loads((out / "fortio.json").read_text())["RetCodes"], codes)
            return result

    def test_scale_out_then_return_to_two(self):
        summary = self.exercise([(9, 2, 2), (140, 4, 2), (101, 5, 4), (12, 2, 2)], {"200": 20})
        self.assertEqual((summary["load"]["peak_ready"], summary["cooldown_ready"]), (4, 2))

    def test_no_scale_out_fails(self):
        with self.assertRaisesRegex(RuntimeError, "third Ready replica"):
            self.exercise([(8, 2, 2), (35, 2, 2), (48, 2, 2)], {"200": 20})

    def test_desired_only_is_insufficient(self):
        with self.assertRaisesRegex(RuntimeError, "third Ready replica"):
            self.exercise([(8, 2, 2), (140, 4, 2), (115, 5, 2)], {"200": 20})

    def test_excess_replica_demand_fails(self):
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.exercise([(8, 2, 2), (180, 6, 3)], {"200": 20})

    def test_http_failures_fail_the_run(self):
        with self.assertRaisesRegex(RuntimeError, "failed requests"):
            self.exercise([(8, 2, 2), (150, 4, 2), (119, 5, 4)], {"200": 19, "503": 1})

    def test_missing_load_report_fails(self):
        with self.assertRaisesRegex(RuntimeError, "JSON report"):
            verify.fortio_result('{"level":"info","msg":"started"}\nthree minutes later\n')


if __name__ == "__main__":
    unittest.main(verbosity=2)
