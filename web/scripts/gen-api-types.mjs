// Regenerates src/api-types.ts from the server's OpenAPI document.
//
//   npm run gen:api-types
//
// Needs `uv` and the server's locked environment (`cd server && uv sync --frozen`).
// The server export opens no database and starts no server. CI runs this and
// fails when the checked-in file differs, so commit the result.
//
// `info.version` is pinned before generating: the server reports the web
// package version (or 0.0.0+dev without a build), and a version bump alone must
// not make the checked-in file stale.

import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";

const webDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const serverDir = resolve(webDir, "../server");
const outFile = resolve(webDir, "src/api-types.ts");

const exported = spawnSync("uv", ["run", "--frozen", "python", "-m", "app.openapi_export"], {
  cwd: serverDir,
  encoding: "utf8",
  stdio: ["ignore", "pipe", "inherit"],
  maxBuffer: 64 * 1024 * 1024,
});
if (exported.error) throw exported.error;
if (exported.status !== 0) {
  console.error(`openapi export failed (exit ${exported.status})`);
  process.exit(exported.status ?? 1);
}

const schema = JSON.parse(exported.stdout);
schema.info = { ...schema.info, version: "0.0.0" };

const ast = await openapiTS(schema, { alphabetize: true });
writeFileSync(outFile, COMMENT_HEADER + astToString(ast));
console.log(`wrote ${outFile}`);
