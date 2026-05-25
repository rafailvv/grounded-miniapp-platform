import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";

const root = path.resolve(new URL("..", import.meta.url).pathname);
const outputFile = path.join(root, ".component-snapshot-tmp.mjs");
const snapshotFile = path.join(root, "tests", "__snapshots__", "workbench-components.json");

const entry = `
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DoctorPanel } from "./src/components/DoctorPanel";
import { ReviewPanel } from "./src/components/ReviewPanel";

const doctorReport = {
  status: "failed",
  checks: [
    { name: "disk_space", status: "passed", details: "free=12.0GB total=64.0GB path=/data; required>=1GB", required: true },
    { name: "docker_daemon", status: "failed", details: "docker daemon did not respond", command: "docker info --format '{{.ServerVersion}}'", required: false }
  ]
};

const review = {
  status: "failed",
  findings: [
    {
      code: "missing_test",
      severity: "high",
      message: "Add workflow regression test.",
      category: "testing",
      source: "guardian",
      file_path: "miniapp/app/static/client/app.js",
      line: 42,
      is_blocker_for_product_acceptance: true
    }
  ],
  summary: {
    finding_count: 1,
    blocker_count: 1,
    missing_tests: 1,
    stale_test_risks: 0,
    browser_proof_gaps: 0,
    contract_mismatches: 0
  },
  evidence: null
};

const suggestions = {
  schema: "grounded.prompt_suggestions.v1",
  status: "ready",
  run_id: "run_1",
  items: [
    {
      suggestion_id: "ps_export",
      title: "Add CSV export",
      category: "export",
      priority: "should",
      reason: "Managers need offline reports.",
      prompt: "Add CSV export for manager orders.",
      target_role: "manager",
      target_files: ["miniapp/app/static/manager/app.js"]
    }
  ]
};

export const snapshots = {
  doctor: renderToStaticMarkup(<DoctorPanel report={doctorReport} />),
  review: renderToStaticMarkup(<ReviewPanel review={review} suggestions={suggestions} />)
};
`;

await build({
  stdin: { contents: entry, resolveDir: root, loader: "tsx" },
  bundle: true,
  platform: "node",
  format: "esm",
  outfile: outputFile,
  external: ["react", "react-dom/server", "react/jsx-runtime"],
});

try {
  const rendered = await import(`${pathToFileURL(outputFile).href}?t=${Date.now()}`);
  const actual = rendered.snapshots;
  const expected = JSON.parse(await fs.readFile(snapshotFile, "utf8"));

  assert.deepEqual(actual, expected);
  for (const [name, html] of Object.entries(actual)) {
    assert.match(html, /workbench-panel/, `${name} snapshot should render Workbench panel markup`);
  }
} finally {
  await fs.rm(outputFile, { force: true });
}
