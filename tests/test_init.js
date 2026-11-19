// Run the actual mongosh bootstrap with a recording database object.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { runInNewContext } = require("node:vm");
const test = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../k8s/database/init-app-user.js"), "utf8");

function bootstrap(env) {
  const created = [];
  const db = {
    getSiblingDB(name) {
      assert.equal(name, "flask_db");
      return {
        getName: () => name,
        createUser: (user) => created.push(JSON.parse(JSON.stringify(user))),
      };
    },
  };
  runInNewContext(source, { db, process: { env } });
  return created;
}

test("creates one readWrite user scoped to flask_db", () => {
  const env = { MONGO_APP_USERNAME: "test_user", MONGO_APP_PASSWORD: "punctuation:/?@%#" };
  assert.deepEqual(bootstrap(env), [{
    user: env.MONGO_APP_USERNAME, pwd: env.MONGO_APP_PASSWORD,
    roles: [{ role: "readWrite", db: "flask_db" }],
  }]);
});
test("requires a username", () => {
  assert.throws(() => bootstrap({ MONGO_APP_PASSWORD: "test" }), /requires application credentials/);
});
test("requires a password", () => {
  assert.throws(() => bootstrap({ MONGO_APP_USERNAME: "test" }), /requires application credentials/);
});
