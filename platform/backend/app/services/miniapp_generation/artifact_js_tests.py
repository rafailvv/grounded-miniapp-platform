from __future__ import annotations

import json
from typing import Any


class ArtifactJsTestsMixin:
    def js_app_level_test_content(self, *, page_graph: dict[str, Any], role_scope: list[str]) -> str:
        roles_literal = json.dumps(list(role_scope), ensure_ascii=True)
        template = r"""import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const MINIAPP_DIR = path.resolve(__dirname, '..');
const APP_DIR = path.join(MINIAPP_DIR, 'app');
const ROUTE_MANIFEST_PATH = path.join(APP_DIR, 'generated', 'route_manifest.json');
const RUNTIME_MANIFEST_PATH = path.join(APP_DIR, 'generated', 'runtime_manifest.json');
const GROUNDED_SPEC_PATH = path.join(MINIAPP_DIR, '..', 'artifacts', 'grounded_spec.json');
const ROLES = __ROLES_LITERAL__;

function loadJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function collectFiles(rootDir, extension) {
  const items = [];
  for (const entry of fs.readdirSync(rootDir, { withFileTypes: true })) {
    const absolutePath = path.join(rootDir, entry.name);
    if (entry.isDirectory()) {
      items.push(...collectFiles(absolutePath, extension));
      continue;
    }
    if (absolutePath.endsWith(extension)) {
      items.push(absolutePath);
    }
  }
  return items;
}

test('generated manifests exist', () => {
  assert.equal(fs.existsSync(ROUTE_MANIFEST_PATH), true, `Missing ${ROUTE_MANIFEST_PATH}`);
  assert.equal(fs.existsSync(RUNTIME_MANIFEST_PATH), true, `Missing ${RUNTIME_MANIFEST_PATH}`);
  const routeManifest = loadJson(ROUTE_MANIFEST_PATH);
  const runtimeManifest = loadJson(RUNTIME_MANIFEST_PATH);
  assert.equal(typeof routeManifest.roles, 'object');
  assert.equal(typeof runtimeManifest.roles, 'object');
});

test('generated javascript files parse', () => {
  const jsFiles = collectFiles(path.join(APP_DIR, 'static'), '.js');
  assert.ok(jsFiles.length > 0, 'No generated JavaScript files found');
  for (const filePath of jsFiles) {
    const result = spawnSync(process.execPath, ['--check', filePath], { encoding: 'utf8' });
    assert.equal(result.status, 0, `node --check failed for ${filePath}\n${result.stderr || result.stdout}`);
  }
});
"""
        return template.replace("__ROLES_LITERAL__", roles_literal)
