import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

from app import create_app


class FlaskContract(unittest.TestCase):
    def setUp(self):
        env = {"MONGODB_URI": "mongodb://mongodb:27017/", "MONGO_APP_USERNAME": "flask_app",
               "MONGO_APP_PASSWORD": "a:/@?b"}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, env, clear=True).start()
        self.driver = patch("app.MongoClient").start()
        self.http = create_app().test_client()
        self.collection = self.driver.return_value["flask_db"]["data"]

    def test_welcome_has_current_utc_time(self):
        response = self.http.get("/")
        prefix = "Welcome to the Flask app! The current time is: "
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertTrue(response.text.startswith(prefix))
        stamp = datetime.fromisoformat(response.text[len(prefix):])
        self.assertLess(abs((datetime.now(timezone.utc) - stamp).total_seconds()), 5)

    def test_app_credentials_are_passed_to_driver_separately(self):
        args, kwargs = self.driver.call_args
        self.assertEqual(args, ("mongodb://mongodb:27017/",))
        self.assertEqual((kwargs["username"], kwargs["password"], kwargs["authSource"]),
                         ("flask_app", "a:/@?b", "flask_db"))

    def test_partial_credentials_fail_at_startup(self):
        del os.environ["MONGO_APP_PASSWORD"]
        with self.assertRaises(ValueError):
            create_app()

    def test_post_and_read_contract(self):
        document = {"sampleKey": "sampleValue", "nested": [1, {"ok": True}]}
        response = self.http.post("/data", json=document)
        self.assertEqual((response.status_code, response.json), (201, {"status": "Data inserted"}))
        self.collection.insert_one.assert_called_once_with(document)
        self.collection.find.return_value = iter([document])
        response = self.http.get("/data")
        self.assertEqual((response.status_code, response.json), (200, [document]))
        self.collection.find.assert_called_once_with({}, {"_id": 0})

    def test_empty_collection(self):
        self.collection.find.return_value = iter(())
        self.assertEqual(self.http.get("/data").json, [])

    def test_invalid_requests_never_insert(self):
        for data, media, code in [(b"null", "application/json", 400),
                                  (b"[]", "application/json", 400),
                                  (b"{", "application/json", 400),
                                  (b"abc", "text/plain", 415),
                                  (b"x" * (1024 * 1024 + 1), "application/json", 413)]:
            with self.subTest(media=media, code=code):
                response = self.http.post("/data", data=data, content_type=media)
                self.assertEqual(response.status_code, code)
                self.assertIn("error", response.json)
        self.collection.insert_one.assert_not_called()

    def test_duplicate_identifier(self):
        self.collection.insert_one.side_effect = DuplicateKeyError("duplicate")
        self.assertEqual(self.http.post("/data", json={"_id": "sample"}).status_code, 409)

    def test_readiness_and_liveness_during_outage(self):
        self.assertEqual(self.http.get("/readyz").status_code, 200)
        self.collection.find_one.assert_called_once_with({}, {"_id": 1})
        self.collection.find_one.side_effect = ServerSelectionTimeoutError("offline")
        self.collection.find.side_effect = ServerSelectionTimeoutError("offline")
        self.assertEqual(self.http.get("/readyz").status_code, 503)
        self.assertEqual(self.http.get("/data").status_code, 503)
        self.assertEqual(self.http.get("/livez").status_code, 200)
        self.assertEqual(self.http.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=2))
