// Integrated security acceptance checks (#71) in headless Chrome:
// 1. cross-origin-attack: an unrelated origin cannot open a session, read jobs, or start a paid job.
// 2. hostile-pinned-viewer: hostile Markdown/HTML/SVG/URLs never execute or navigate unsafely,
//    while legitimate pinned GitHub #Lx-Ly source links stay clickable.
import puppeteer from 'puppeteer';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [scenario, baseUrl, browserPath, extraArg, flag] = process.argv.slice(2);

async function withBrowser(fn) {
  const profile = await mkdtemp(join(tmpdir(), 'repodocs-sec71-'));
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
  const externalRequests = [];

  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('dialog', async (d) => {
    dialogs.push(d.message());
    await d.dismiss();
  });
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('request', (req) => {
    const url = req.url();
    if (!url.startsWith('http://127.0.0.1:') && !url.startsWith('data:') && !url.startsWith('blob:')) {
      externalRequests.push(url);
    }
  });

  if (scenario === 'cross-origin-attack') {
    // extraArg is JSON: { attackerUrl, workspaceId, jobId, validJobBody }
    const cfg = JSON.parse(extraArg);
    // Step 1: Establish a genuine victim session on the RepoDocs origin (positive control in browser).
    await page.goto(baseUrl, { waitUntil: 'networkidle0', timeout: 20000 });
    await clickButton(page, 'Open session');
    await waitForText(page, 'Select source repositories');

    // Step 2: Navigate in the same browser context (holding the victim cookie) to the unrelated attacker origin.
    // Attacker JS receives NO victim CSRF token or API key; only public target URLs/IDs.
    await page.goto(cfg.attackerUrl, { waitUntil: 'networkidle0', timeout: 20000 });
    const attackResults = await page.evaluate(async (targetBase, jobId, validBody) => {
      const results = {};
      // 1) Preflighted JSON POST with otherwise-valid payload
      try {
        const resp = await fetch(`${targetBase}/api/v1/jobs`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(validBody),
        });
        results.preflightedJob = { readable: true, status: resp.status };
      } catch (err) {
        results.preflightedJob = { readable: false, error: String(err) };
      }

      // 2) Simple/no-preflight text/plain CORS POST with otherwise-valid payload
      try {
        const resp = await fetch(`${targetBase}/api/v1/jobs`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify(validBody),
        });
        results.simpleCorsJob = { readable: true, status: resp.status };
      } catch (err) {
        results.simpleCorsJob = { readable: false, error: String(err) };
      }

      // 3) Simple/no-preflight no-cors POST (opaque response; server must still reject)
      try {
        const resp = await fetch(`${targetBase}/api/v1/jobs`, {
          method: 'POST',
          mode: 'no-cors',
          credentials: 'include',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify(validBody),
        });
        results.noCorsJob = { type: resp.type, status: resp.status, ok: resp.ok };
      } catch (err) {
        results.noCorsJob = { error: String(err) };
      }

      // 4) Simple GET on victim's protected job endpoint
      try {
        const resp = await fetch(`${targetBase}/api/v1/jobs/${jobId}`, {
          method: 'GET',
          credentials: 'include',
        });
        results.readJob = { readable: true, status: resp.status };
      } catch (err) {
        results.readJob = { readable: false, error: String(err) };
      }

      return results;
    }, baseUrl, cfg.jobId, cfg.validJobBody);

    return { ok: true, attackResults, dialogs, pageErrors, externalRequests };
  }

  if (scenario === 'hostile-pinned-viewer') {
    await page.goto(baseUrl, { waitUntil: 'networkidle0', timeout: 20000 });
    await clickButton(page, 'Open session');
    await waitForText(page, 'Select source repositories');

    await page.evaluate(() => {
      const input = document.querySelector('input[aria-label="Local source name"]');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, 'shop');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
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
    await waitForText(page, 'example/shop@');

    // Click rendered SVG node to open its evidence and verify pinned GitHub #L1-L... link
    await page.evaluate(() => {
      const node = document.querySelector('svg g.node[role="button"]');
      node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
    await page.waitForFunction(
      () => Boolean(document.querySelector('[aria-label="Selected component evidence"]')),
      { timeout: 10000 },
    );

    const links = await page.evaluate(() =>
      Array.from(document.querySelectorAll('a[href]')).map((a) => ({
        href: a.getAttribute('href') || '',
        rel: a.getAttribute('rel') || '',
        text: a.textContent || '',
      })),
    );
    const bodyText = await page.evaluate(() => document.body.innerText);
    return { ok: true, links, bodyText, dialogs, pageErrors, consoleErrors, externalRequests };
  }

  throw new Error(`Unknown scenario: ${scenario}`);
});

console.log(JSON.stringify(report));
