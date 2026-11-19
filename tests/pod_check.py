"""Executed over stdin inside each Flask pod by scripts/verify.py."""
import json
import os
import socket
import sys
from urllib.request import ProxyHandler, build_opener

from pymongo import MongoClient
from pymongo.errors import OperationFailure

marker, expected_ip = sys.argv[1:]
for host in ("mongodb", "mongodb.faralpha.svc.cluster.local"):
    ips = {entry[4][0] for entry in socket.getaddrinfo(host, 27017, type=socket.SOCK_STREAM)}
    if expected_ip not in ips:
        raise RuntimeError(f"{host} resolved to {ips}, expected the ready MongoDB pod {expected_ip}")

http = build_opener(ProxyHandler({}))
with http.open("http://127.0.0.1:5000/data", timeout=8) as response:
    rows = json.load(response)
if not any(row.get("faralphaRun") == marker and "_id" not in row for row in rows):
    raise RuntimeError("This Flask replica did not return the shared test record.")

uri = os.environ["MONGODB_URI"]
auth = dict(username=os.environ["MONGO_APP_USERNAME"], password=os.environ["MONGO_APP_PASSWORD"],
            authSource="flask_db")


def require_denial(expected_code, operation):
    try:
        operation()
    except OperationFailure as exc:
        if exc.code == expected_code:
            return
        raise
    raise RuntimeError(f"MongoDB allowed an operation expected to fail with code {expected_code}.")


with MongoClient(uri, timeoutMS=4000) as anonymous:
    require_denial(13, lambda: anonymous.flask_db.data.find_one({}))
with MongoClient(uri, timeoutMS=4000, **dict(auth, password=auth["password"] + "-wrong")) as invalid:
    require_denial(18, lambda: invalid.flask_db.data.find_one({}))
with MongoClient(uri, timeoutMS=4000, **auth) as client:
    if client.flask_db.data.find_one({"faralphaRun": marker}) is None:
        raise RuntimeError("The application account could not read the marker directly.")
    require_denial(13, lambda: client.admin.command({"usersInfo": 1}))

print(f"{socket.gethostname()} | both DNS names resolve to {expected_ip} | shared row found | "
      "anonymous read 13 | invalid password 18 | admin operation 13")
