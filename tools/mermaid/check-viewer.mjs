// Drives the real RepoDocs React viewer against a live local HTTP server in headless Chrome.
// Usage: node check-viewer.mjs <baseUrl> <browserPath> [--no-sandbox|--]
import puppeteer from 'puppeteer';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [baseUrl, browserPath, flag] = process.argv.slice(2);

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
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });

  await page.goto(baseUrl, { waitUntil: 'networkidle0', timeout: 20000 });
  await clickButton(page, 'Open session');
  await waitForText(page, 'Select source repositories');

  // Fill local source 'shop'
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

  // Select Architecture mode + components view
  await page.evaluate(() => {
    const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
    const arch = radios.find((r) => r.parentElement && r.parentElement.textContent.includes('Architecture'));
    arch.click();
  });
  await page.evaluate(() => {
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

  // Wait for SVG diagram and evidence map to render
  await page.waitForFunction(() => Boolean(document.querySelector('svg')), { timeout: 20000 });
  await waitForText(page, 'Components (');
  await waitForText(page, 'working-tree (unverified)');

  // Click Fit button
  await clickButton(page, 'Fit');

  // Click first component button in EvidencePanel
  await page.evaluate(() => {
    const aside = document.querySelector('aside[aria-label="Evidence panel"]');
    const btn = aside.querySelector('ul button');
    if (!btn) throw new Error('No component button in EvidencePanel');
    btn.click();
  });
  await waitForText(page, 'Support:');
  await waitForText(page, 'Evidence:');

  const details = await page.evaluate(() => ({
    svgCount: document.querySelectorAll('svg').length,
    downloadLinks: Array.from(document.querySelectorAll('section[aria-label="Artifact downloads"] a[download]')).map(
      (a) => a.getAttribute('download'),
    ),
    revisionText: document.querySelector('[aria-label="Repository revisions"]')?.textContent || '',
    bodyText: document.body.innerText,
  }));

  return { ok: true, details, pageErrors, consoleErrors };
});

console.log(JSON.stringify(report));
