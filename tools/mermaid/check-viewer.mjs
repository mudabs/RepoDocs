// Drives the real RepoDocs React viewer against a live local HTTP server in headless Chrome.
// Usage: node check-viewer.mjs <baseUrl> <browserPath> <scenario> [--no-sandbox|--]
import puppeteer from 'puppeteer';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [baseUrl, browserPath, scenario, flag] = process.argv.slice(2);

async function withBrowser(fn) {
  const profile = await mkdtemp(join(tmpdir(), 'repodocs-viewer-e2e-'));
  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: browserPath,
      headless: true,
      userDataDir: profile,
      args: flag === '--no-sandbox' ? ['--no-sandbox'] : [],
    });
    return await fn(browser);
  } finally {
    if (browser) {
      await browser.close().catch(() => {});
    }
    await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

async function clickButton(page, text) {
  await page.waitForFunction(
    (target) => Array.from(document.querySelectorAll('button')).some((b) => !b.disabled && b.textContent.trim() === target),
    { timeout: 15000 },
    text,
  );
  await page.evaluate((target) => {
    const btn = Array.from(document.querySelectorAll('button')).find((b) => !b.disabled && b.textContent.trim() === target);
    if (!btn) throw new Error(`Button not found: ${target}`);
    btn.click();
  }, text);
}

async function waitForText(page, text, timeout = 25000) {
  await page.waitForFunction(
    (t) => document.body && document.body.innerText.includes(t),
    { timeout },
    text,
  );
}

const report = await withBrowser(async (browser) => {
  const page = await browser.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  const dialogs = [];
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('dialog', async (d) => {
    dialogs.push(d.message());
    await d.dismiss();
  });
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });

  await page.goto(baseUrl, { waitUntil: 'networkidle0', timeout: 20000 });
  await clickButton(page, 'Open session');
  await waitForText(page, 'Select source repositories');

  const fillLocal = async (name) => {
    await page.evaluate((val) => {
      const input = document.querySelector('input[aria-label="Local source name"]');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, val);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }, name);
  };

  if (scenario === 'cross-repo-viewer') {
    await fillLocal('shop');
    await clickButton(page, 'Add source');
    await fillLocal('shop2');
    await clickButton(page, 'Add source');
  } else {
    await fillLocal('shop');
  }
  await clickButton(page, 'Create workspace');
  await waitForText(page, 'Scan and preview');
  await clickButton(page, 'Scan and preview');
  await waitForText(page, 'Select mode');

  await page.evaluate(() => {
    const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
    const arch = radios.find((r) => r.parentElement && r.parentElement.textContent.includes('Architecture'));
    arch.click();
    const checks = Array.from(document.querySelectorAll('input[type="checkbox"]'));
    const comp = checks.find((c) => c.parentElement && c.parentElement.textContent.includes('components'));
    if (comp && !comp.checked) comp.click();
  });
  await clickButton(page, 'Continue');
  await waitForText(page, 'Connect provider');

  await page.evaluate(() => {
    const input = document.querySelector('input[aria-label="Model name"]');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, 'replay-model');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await clickButton(page, 'Connect provider');
  await waitForText(page, 'Provider connected');
  await clickButton(page, 'Continue');
  await waitForText(page, 'Budget limits');
  await clickButton(page, 'Continue');
  await waitForText(page, 'Submit job');
  await clickButton(page, 'Submit job');
  await waitForText(page, 'Succeeded');
  await clickButton(page, 'View diagrams');

  await page.waitForFunction(() => Boolean(document.querySelector('svg g.node[role="button"]')), { timeout: 20000 });
  await waitForText(page, 'Components (');
  await waitForText(page, 'working-tree (unverified)');

  // 1. Click Fit button
  await clickButton(page, 'Fit');

  // 2. Click a rendered diagram NODE directly in the SVG (not sidebar)
  await page.evaluate(() => {
    const node = document.querySelector('svg g.node[role="button"]');
    if (!node) throw new Error('No clickable g.node in SVG');
    node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  });
  await page.waitForFunction(
    () => Boolean(document.querySelector('[aria-label="Selected component evidence"]')),
    { timeout: 10000 },
  );
  const nodePanelText = await page.evaluate(
    () => document.querySelector('[aria-label="Selected component evidence"]').textContent || '',
  );

  // 3. Click and keyboard-activate every rendered diagram EDGE directly in the SVG
  const edgeCount = await page.evaluate(() => document.querySelectorAll('svg g.edgePaths path[role="button"]').length);
  if (edgeCount < 1) throw new Error('Expected at least 1 clickable edge path in SVG');

  const edgeSelections = [];
  for (let idx = 0; idx < edgeCount; idx += 1) {
    const mode = idx % 2 === 0 ? 'click' : 'keydown';
    const expectedIds = await page.evaluate((edgeIdx, action) => {
      const edges = document.querySelectorAll('svg g.edgePaths path[role="button"]');
      const edge = edges[edgeIdx];
      const connAttr = edge.getAttribute('data-edge-connections') || '';
      if (action === 'click') {
        edge.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      } else {
        edge.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      }
      return connAttr.split(',').filter(Boolean);
    }, idx, mode);

    await page.waitForFunction(
      () => Boolean(
        document.querySelector('[aria-label="Selected connection evidence"]') ||
        document.querySelector('[aria-label="Selected edge evidence"]'),
      ),
      { timeout: 10000 },
    );

    const observed = await page.evaluate(() => {
      const cards = Array.from(document.querySelectorAll('[data-selected-connection-id]'));
      return {
        selectedIds: cards.map((c) => c.getAttribute('data-selected-connection-id')),
        text: (
          document.querySelector('[aria-label="Selected connection evidence"]') ||
          document.querySelector('[aria-label="Selected edge evidence"]')
        )?.textContent || '',
      };
    });
    edgeSelections.push({ index: idx, mode, expectedIds, ...observed });
  }
  const edgePanelText = edgeSelections.map((s) => s.text).join('\n');

  // 4. Perform actual artifact download fetches from the rendered download anchors
  const downloaded = await page.evaluate(async () => {
    const anchors = Array.from(document.querySelectorAll('section[aria-label="Artifact downloads"] a[download]'));
    const results = [];
    for (const a of anchors) {
      const resp = await fetch(a.getAttribute('href'), { credentials: 'include' });
      const text = await resp.text();
      results.push({
        name: a.getAttribute('download'),
        status: resp.status,
        disposition: resp.headers.get('content-disposition') || '',
        bytes: text.length,
      });
    }
    return results;
  });

  // 5. Inspect all rendered links to ensure no javascript: or non-allowlisted external hrefs exist
  const badLinks = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a[href]'))
      .map((a) => a.getAttribute('href') || '')
      .filter((href) => href.startsWith('javascript:') || href.includes('evil.example')),
  );

  const details = await page.evaluate(() => ({
    svgCount: document.querySelectorAll('svg').length,
    revisionText: document.querySelector('[aria-label="Repository revisions"]')?.textContent || '',
    bodyText: document.body.innerText,
  }));

  return {
    ok: true,
    edgeCount,
    edgeSelections,
    nodePanelText,
    edgePanelText,
    downloaded,
    badLinks,
    dialogs,
    details,
    pageErrors,
    consoleErrors,
  };
});

console.log(JSON.stringify(report));
