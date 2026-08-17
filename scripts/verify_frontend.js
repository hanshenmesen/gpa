#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const webRoot = path.join(root, 'demo_web');
const htmlFiles = ['index.html', 'store.html', 'control.html', 'setup.html', 'case_lab.html', 'tutorial_lab.html'];
const sharedScripts = ['environment.js', 'product.js'];
const failures = [];

function fail(message) {
  failures.push(message);
}

for (const scriptName of sharedScripts) {
  const scriptPath = path.join(webRoot, scriptName);
  try {
    new Function(fs.readFileSync(scriptPath, 'utf8'));
  } catch (error) {
    fail(`${scriptName}: ${error.message}`);
  }
}

try {
  const listeners = {};
  const runtime = {
    navigator: { language: 'zh-CN', userAgent: 'Mozilla/5.0 Chrome/120.0' },
    window: {
      screen: { width: 1512, height: 982 },
      innerWidth: 1200,
      innerHeight: 800,
      devicePixelRatio: 2,
      setTimeout,
      clearTimeout,
      addEventListener() {},
      removeEventListener() {},
    },
    document: {
      hidden: false,
      addEventListener(name, listener) { listeners[name] = listener; },
      removeEventListener() {},
    },
    Intl,
    URLSearchParams,
    AbortController,
  };
  vm.runInNewContext(fs.readFileSync(path.join(webRoot, 'environment.js'), 'utf8'), runtime);
  const client = runtime.GPAClient;
  const expectedMethods = [
    'environmentSnapshot', 'environmentQuery', 'requestJson', 'postJson',
    'sendJsonBeacon', 'createVisibilityPoller',
  ];
  for (const method of expectedMethods) {
    if (typeof client?.[method] !== 'function') fail(`environment.js: missing GPAClient.${method}`);
  }
  const snapshot = client?.environmentSnapshot();
  if (snapshot?.browser?.family !== 'Chrome') fail('environment.js: browser family detection failed');
  if (snapshot?.screen?.pixel_ratio !== 2) fail('environment.js: screen snapshot failed');
  const query = client?.environmentQuery(snapshot);
  if (query?.get('viewport_width') !== '1200') fail('environment.js: environment query failed');

  let concurrent = 0;
  let maxConcurrent = 0;
  let releaseFirst;
  const firstRun = new Promise(resolve => { releaseFirst = resolve; });
  const stopPoller = client?.createVisibilityPoller(async () => {
    concurrent += 1;
    maxConcurrent = Math.max(maxConcurrent, concurrent);
    if (maxConcurrent === 1) await firstRun;
    concurrent -= 1;
  }, { runImmediately: true, activeMs: 250 });
  listeners.visibilitychange?.();
  releaseFirst();
  stopPoller?.();
  if (maxConcurrent !== 1) fail('environment.js: visibility poller ran overlapping requests');

  const recordedTimeouts = [];
  runtime.window.setTimeout = (callback, duration) => {
    recordedTimeouts.push(duration);
    return setTimeout(callback, 60000);
  };
  runtime.fetch = async () => ({ ok: true, text: async () => '{}' });
  void client?.requestJson('/bounded-timeout', { timeoutMs: Infinity });
  if (recordedTimeouts.at(-1) !== 60000) {
    fail('environment.js: non-finite request timeout was not bounded');
  }
} catch (error) {
  fail(`environment.js: runtime contract failed: ${error.message}`);
}

for (const htmlName of htmlFiles) {
  const html = fs.readFileSync(path.join(webRoot, htmlName), 'utf8');
  const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map(match => match[1]);
  const duplicates = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
  if (duplicates.length) fail(`${htmlName}: duplicate ids: ${duplicates.join(', ')}`);

  const localTargets = [...html.matchAll(/\bhref="#([^"]+)"/g)].map(match => match[1]);
  const missingTargets = [...new Set(localTargets.filter(target => !ids.includes(target)))];
  if (missingTargets.length) fail(`${htmlName}: missing hash targets: ${missingTargets.join(', ')}`);

  for (const attribute of ['aria-labelledby', 'aria-describedby']) {
    const references = [...html.matchAll(new RegExp(`\\b${attribute}="([^"]+)"`, 'g'))]
      .flatMap(match => match[1].trim().split(/\s+/))
      .filter(Boolean);
    const missingReferences = [...new Set(references.filter(target => !ids.includes(target)))];
    if (missingReferences.length) {
      fail(`${htmlName}: ${attribute} references missing ids: ${missingReferences.join(', ')}`);
    }
  }

  const dialogs = [
    ...html.matchAll(/<dialog\b([^>]*)>/gi),
    ...html.matchAll(/<[^>]+\brole="dialog"([^>]*)>/gi),
  ];
  dialogs.forEach((match, index) => {
    if (!/\baria-(?:label|labelledby)="[^"]+"/i.test(match[1])) {
      fail(`${htmlName}: dialog ${index + 1} is missing an accessible name`);
    }
  });

  const inlineScripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
  inlineScripts.forEach((match, index) => {
    try {
      new Function(match[1]);
    } catch (error) {
      fail(`${htmlName}: inline script ${index + 1}: ${error.message}`);
    }
  });

  const assetPaths = [...html.matchAll(/(?:src|href)="\/assets\/([^"]+)"/g)]
    .map(match => match[1]);
  for (const assetPath of assetPaths) {
    if (!fs.existsSync(path.join(webRoot, assetPath))) {
      fail(`${htmlName}: missing asset /assets/${assetPath}`);
    }
  }
}

const storeHtml = fs.readFileSync(path.join(webRoot, 'store.html'), 'utf8');
if (storeHtml.includes('选择一个 Replay')) {
  fail('store.html: persistent empty detail copy should not occupy the catalog');
}
if (!/<aside[^>]+id="recordDetail"[^>]+hidden/.test(storeHtml)) {
  fail('store.html: Replay detail panel must be hidden until a record is selected');
}
const studioHtml = fs.readFileSync(path.join(webRoot, 'index.html'), 'utf8');
if (!studioHtml.includes('step.metadata?.intent_normalization')) {
  fail('index.html: merged recording steps do not expose intent normalization evidence');
}

const css = fs.readFileSync(path.join(webRoot, 'product.css'), 'utf8');
const product = fs.readFileSync(path.join(webRoot, 'product.js'), 'utf8');
if (!product.includes('window.clearTimeout(commandCloseTimer)')) {
  fail('product.js: command palette close timer is not cancelled before reopening');
}
let depth = 0;
let quote = null;
let comment = false;
for (let index = 0; index < css.length; index += 1) {
  const current = css[index];
  const next = css[index + 1];
  if (comment) {
    if (current === '*' && next === '/') {
      comment = false;
      index += 1;
    }
    continue;
  }
  if (!quote && current === '/' && next === '*') {
    comment = true;
    index += 1;
    continue;
  }
  if (quote) {
    if (current === '\\') index += 1;
    else if (current === quote) quote = null;
    continue;
  }
  if (current === '"' || current === "'") quote = current;
  else if (current === '{') depth += 1;
  else if (current === '}') depth -= 1;
  if (depth < 0) fail(`product.css: unexpected closing brace at byte ${index}`);
}
if (depth !== 0) fail(`product.css: unbalanced braces (${depth})`);
if (quote) fail('product.css: unterminated string');
if (comment) fail('product.css: unterminated comment');
if (!css.includes('body:not(.store-detail-open) .store-layout > .detail-panel')) {
  fail('product.css: Store detail visibility does not follow explicit application state');
}

if (failures.length) {
  failures.forEach(message => process.stderr.write(`- ${message}\n`));
  process.exit(1);
}
process.stdout.write(`Validated ${htmlFiles.length} pages, ${sharedScripts.length} scripts, and shared CSS.\n`);
