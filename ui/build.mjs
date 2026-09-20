/**
 * Compile the TypeScript app into the two files `writ serve` ships.
 *
 * Why a script instead of a bundler: the whole app is a handful of ES modules
 * with no third-party imports, so "bundling" is concatenation in dependency
 * order. A bundler would add a dependency, a lockfile, and a version to keep
 * current, in exchange for nothing this needs.
 *
 * Why check the output into git: `pip install writ` must not require node. The
 * built assets live in `writ/static/`, are imported by `writ/server.py`, and a
 * test asserts they are in step with `ui/src` so a stale build cannot ship
 * unnoticed.
 *
 * Usage:
 *   node ui/build.mjs          compile, typecheck, and write writ/static/
 *   node ui/build.mjs --check  verify the committed output is current
 */

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const srcDir = join(here, 'src');
const buildDir = join(here, 'build');
const outDir = join(root, 'writ', 'static');

/** Dependency order, so concatenation is a valid module-free script. */
const ORDER = [
  'types.js',
  'dom.js',
  'format.js',
  'store.js',
  'views/graph.js',
  'views/overview.js',
  'views/tasks.js',
  'views/runs.js',
  'views/decisions.js',
  'views/milestones.js',
  'views/plan.js',
  'app.js',
];

function typecheckAndCompile() {
  rmSync(buildDir, { recursive: true, force: true });
  // tsc from node_modules if present, otherwise whatever is on PATH: a
  // contributor with a global tsc should not need an npm install.
  const local = join(here, 'node_modules', '.bin', 'tsc');
  const bin = existsSync(local) ? local : 'tsc';
  execFileSync(bin, ['--project', join(here, 'tsconfig.json')], {
    stdio: 'inherit',
    cwd: here,
  });
}

/**
 * Strip the import/export lines and concatenate.
 *
 * Every import here is relative and every module is included, so the names all
 * end up in one scope. Type-only imports are already gone by this point;
 * `verbatimModuleSyntax` means what is left is real, and all of it is internal.
 *
 * That shared scope is the one real cost of not running a bundler: two modules
 * may each define a private helper of the same name, and the later one silently
 * wins. That is not hypothetical — `overview` and `decisions` both had a `card`,
 * and the overview rendered decisions. So collisions are a build error.
 */
/**
 * Every module under src is in ORDER.
 *
 * ORDER is written by hand because concatenation needs a dependency order, and
 * nothing else checks it. A file left out still typechecks, still gets hashed into
 * the source stamp — so the staleness test reads as current — and is simply absent
 * from the bundle: a whole view that renders nothing, with every check passing.
 * That happened, which is why this is here.
 */
function assertEveryModuleIsOrdered() {
  const found = [];
  const walk = (dir, prefix = '') => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.isDirectory()) walk(join(dir, entry.name), `${prefix}${entry.name}/`);
      else if (entry.name.endsWith('.ts')) found.push(`${prefix}${entry.name.replace(/\.ts$/, '.js')}`);
    }
  };
  walk(srcDir);
  const missing = found.filter((name) => !ORDER.includes(name));
  if (missing.length) {
    console.error('these modules are under ui/src but not in ORDER, so they would be');
    console.error('compiled and then silently left out of the bundle:');
    for (const name of missing) console.error(`  ${name}`);
    console.error('add each one to ORDER at the position its dependencies allow.');
    process.exit(1);
  }
}

function bundle() {
  const parts = [];
  const seen = new Map();
  const clashes = [];
  for (const name of ORDER) {
    const path = join(buildDir, name);
    const body = readFileSync(path, 'utf8')
      .split('\n')
      .filter((line) => !/^\s*import\s/.test(line) && !/^\s*export\s*\{[^}]*\}\s*;?\s*$/.test(line))
      .map((line) => line.replace(/^export\s+(const|function|class|let|var)\s/, '$1 '))
      .join('\n');
    for (const declared of topLevelNames(body)) {
      const previous = seen.get(declared);
      if (previous) clashes.push(`${declared}: ${previous} and ${name}`);
      else seen.set(declared, name);
    }
    parts.push(`// ---- ${name} ----\n${body.trim()}\n`);
  }
  if (clashes.length) {
    console.error('these top-level names are defined twice, so one would shadow the other:');
    for (const clash of clashes) console.error(`  ${clash}`);
    console.error('rename one, or make it a method — the bundle is a single scope.');
    process.exit(1);
  }
  return [
    '/*',
    ' * writ dashboard — compiled from ui/src by ui/build.mjs.',
    ' * Do not edit: change the TypeScript and rebuild.',
    ' */',
    '(() => {',
    '"use strict";',
    parts.join('\n'),
    '})();',
    '',
  ].join('\n');
}

/**
 * Top-level declarations only: a line starting at column zero.
 *
 * tsc's output is consistently indented, so nesting is unambiguous without
 * parsing. A helper inside a function is scoped and cannot collide.
 */
function topLevelNames(body) {
  const names = [];
  for (const line of body.split('\n')) {
    const match = /^(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)/.exec(line);
    if (match) names.push(match[1]);
  }
  return names;
}

function fingerprint(text) {
  return createHash('sha256').update(text).digest('hex').slice(0, 12);
}

/** Every .ts and .css under src, hashed, so drift is detectable. */
function sourceHash() {
  const files = [];
  const walk = (dir, prefix = '') => {
    for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) =>
      a.name.localeCompare(b.name),
    )) {
      const next = join(dir, entry.name);
      if (entry.isDirectory()) walk(next, `${prefix}${entry.name}/`);
      else if (/\.(ts|css)$/.test(entry.name)) {
        files.push(`${prefix}${entry.name}:${createHash('sha256').update(readFileSync(next)).digest('hex')}`);
      }
    }
  };
  walk(srcDir);
  return createHash('sha256').update(files.join('\n')).digest('hex').slice(0, 12);
}

function main() {
  const check = process.argv.includes('--check');
  typecheckAndCompile();

  const script = bundle();
  const css = readFileSync(join(srcDir, 'style.css'), 'utf8');
  assertEveryModuleIsOrdered();
  const stamp = sourceHash();
  const header = `/* built from ui/src (${stamp}) */\n`;

  if (check) {
    const existing = readFileSync(join(outDir, 'app.js'), 'utf8');
    if (!existing.startsWith(header)) {
      console.error(`writ/static is stale: sources hash ${stamp}`);
      console.error('run: node ui/build.mjs');
      process.exit(1);
    }
    console.log(`writ/static is current (${stamp})`);
    return;
  }

  mkdirSync(outDir, { recursive: true });
  writeFileSync(join(outDir, 'app.js'), header + script);
  writeFileSync(join(outDir, 'style.css'), header + css);
  console.log(`wrote writ/static/app.js (${script.length} bytes) and style.css (${css.length} bytes)`);
  console.log(`source stamp ${stamp}`);
}

main();
