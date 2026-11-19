// The official image runs this file in mongosh on an empty data directory.
const username = process.env.MONGO_APP_USERNAME;
const password = process.env.MONGO_APP_PASSWORD;
if (!username || !password) {
  throw new Error("MongoDB initialization requires application credentials.");
}
const appDb = db.getSiblingDB("flask_db");
appDb.createUser({
  user: username,
  pwd: password,
  roles: [{ role: "readWrite", db: appDb.getName() }],
});
