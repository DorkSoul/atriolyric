// The dependency audit the release gate runs (npm run audit:deps).
//
// `npm audit --audit-level=high` with two differences:
//   - packages that ship in the bundle (`--omit=dev`) get no exceptions: a
//     high or critical advisory there always fails;
//   - an advisory listed in audit-exceptions.json is let through for
//     development-only packages until its `expires` date. npm has no way to
//     accept one advisory, and the alternatives are worse: lowering the
//     level hides every other finding, and a gate that cannot pass gets
//     bypassed. Each entry says why it is safe and when to look again; an
//     expired or unused entry fails the audit, so the list cannot rot.

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SERIOUS = new Set(["high", "critical"]);

function audit(extraArgs) {
  const result = spawnSync("npm", ["audit", "--json", ...extraArgs], { cwd: webDir, encoding: "utf8" });
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch {
    console.error(result.stdout, result.stderr);
    throw new Error("npm audit did not answer with JSON (registry unreachable?)");
  }
  if (report.error) throw new Error(`npm audit failed: ${report.error.summary ?? JSON.stringify(report.error)}`);
  return report.vulnerabilities ?? {};
}

/** Every serious advisory behind the report, as { id, package, title }. */
function seriousAdvisories(vulnerabilities) {
  const found = new Map();
  for (const entry of Object.values(vulnerabilities)) {
    for (const via of entry.via ?? []) {
      // A string names another vulnerable package, whose own entry is listed too.
      if (typeof via === "string" || !SERIOUS.has(via.severity)) continue;
      const id = /GHSA-[0-9a-z-]+/i.exec(via.url ?? "")?.[0] ?? String(via.source);
      found.set(id, { id, package: via.name, title: via.title });
    }
  }
  return [...found.values()];
}

const { exceptions } = JSON.parse(readFileSync(resolve(webDir, "audit-exceptions.json"), "utf8"));
const today = new Date().toISOString().slice(0, 10);
const problems = [];

for (const advisory of seriousAdvisories(audit(["--omit=dev"]))) {
  problems.push(`${advisory.id} (${advisory.package}: ${advisory.title}) is in a package that ships. No exception applies there.`);
}

const used = new Set();
for (const advisory of seriousAdvisories(audit([]))) {
  const exception = exceptions.find((entry) => entry.advisory === advisory.id);
  if (!exception) {
    problems.push(`${advisory.id} (${advisory.package}: ${advisory.title})`);
  } else if (exception.expires < today) {
    problems.push(`${advisory.id}: its exception in audit-exceptions.json expired on ${exception.expires}. Look again, then fix it or renew the date.`);
  } else {
    used.add(exception.advisory);
    console.log(`Accepted until ${exception.expires}: ${advisory.id} (${advisory.package}, development only)`);
  }
}
for (const exception of exceptions) {
  if (!used.has(exception.advisory)) {
    problems.push(`${exception.advisory}: listed in audit-exceptions.json but no longer reported. Remove the entry.`);
  }
}

if (problems.length) {
  console.error("Dependency audit failed:\n  " + problems.join("\n  "));
  process.exit(1);
}
console.log("Dependency audit passed: no high or critical advisory outside the accepted ones.");
