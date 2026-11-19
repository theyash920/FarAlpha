import os
from datetime import datetime, timezone

from bson.errors import InvalidDocument
from flask import Flask, Response, jsonify, request
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError, WriteError
from werkzeug.exceptions import HTTPException


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    username = os.environ.get("MONGO_APP_USERNAME")
    password = os.environ.get("MONGO_APP_PASSWORD")
    credentials = {}
    if username is not None or password is not None:
        if not username or not password:
            raise ValueError("Set both MONGO_APP_USERNAME and MONGO_APP_PASSWORD.")
        credentials = {"username": username, "password": password, "authSource": "flask_db"}

    client = MongoClient(
        os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017/"),
        **credentials,
        connect=False,
        serverSelectionTimeoutMS=2000,
        timeoutMS=3000,
        maxPoolSize=8,
    )
    collection = client["flask_db"]["data"]

    @app.get("/")
    def index():
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return Response(f"Welcome to the Flask app! The current time is: {now}", mimetype="text/plain")

    @app.get("/data")
    def list_data():
        return jsonify(list(collection.find({}, {"_id": 0})))

    @app.post("/data")
    def insert_data():
        document = request.get_json()
        if not isinstance(document, dict):
            return jsonify(error="Send a JSON object."), 400
        try:
            collection.insert_one(document)
        except DuplicateKeyError:
            return jsonify(error="A document with that key already exists."), 409
        except (InvalidDocument, OverflowError):
            return jsonify(error="The document contains a value MongoDB cannot store."), 400
        except WriteError as exc:
            if exc.code in (2, 52, 53, 121):
                return jsonify(error="MongoDB rejected the document."), 400
            raise
        return jsonify(status="Data inserted"), 201

    @app.get("/livez")
    def live():
        return jsonify(status="alive")

    @app.get("/readyz")
    def ready():
        # An authorized read works on an empty collection and exercises the same credentials as /data.
        collection.find_one({}, {"_id": 1})
        return jsonify(status="ready")

    @app.errorhandler(PyMongoError)
    def database_unavailable(exc):
        app.logger.warning("Database operation failed (%s)", type(exc).__name__)
        return jsonify(error="Database unavailable."), 503

    @app.errorhandler(HTTPException)
    def http_error(exc):
        response = exc.get_response()
        response.data = app.json.dumps({"error": exc.description})
        response.content_type = "application/json"
        return response

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000)
