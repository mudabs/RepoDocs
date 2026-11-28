// Drives the real RepoDocs React/Vite frontend against a live local HTTP server in headless Chrome.
// Usage: node check-web-workflow.mjs <baseUrl> <browserPath> <scenario> [--no-sandbox|--]
import puppeteer from 'puppeteer';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [baseUrl, browserPath, scenario, flag] = process.argv.slice(2);
const CANARY = 'sk-ant-canary-0123456789abcdefghijklmnopqrstuv';

async function withBrowser(fn) {
  const profile = await mkdtemp(join(tmpdir(), 'repodocs-web-e2e-'));
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
  const clicked = await page.evaluate((target) => {
    const btn = Array.from(document.querySelectorAll('button')).find((b) => !b.disabled && b.textContent.trim() === target);
    if (!btn) return false;
    btn.click();
    return true;
  }, text);
  if (!clicked) throw new Error(`Button not found or disabled: ${text}`);
}

async function typeIntoLabel(page, labelFragment, value) {
  const ok = await page.evaluate((frag, val) => {
    const labels = Array.from(document.querySelectorAll('label'));
    for (const lbl of labels) {
      if (lbl.textContent.includes(frag)) {
        const input = lbl.querySelector('input');
        if (input) {
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          setter.call(input, val);
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }
      }
    }
    return false;
  }, labelFragment, value);
  if (!ok) throw new Error(`Input for label "${labelFragment}" not found`);
}

async function clickRadioByLabel(page, textFragment) {
  const ok = await page.evaluate((frag) => {
    const labels = Array.from(document.querySelectorAll('label'));
    for (const lbl of labels) {
      if (lbl.textContent.includes(frag)) {
        const input = lbl.querySelector('input[type="radio"]');
        if (input) {
          input.click();
          return true;
        }
      }
    }
    return false;
  }, textFragment);
  if (!ok) throw new Error(`Radio for "${textFragment}" not found`);
}

async function checkBoxByAria(page, ariaFragment) {
  const ok = await page.evaluate((frag) => {
    const inputs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
    for (const inp of inputs) {
      if ((inp.getAttribute('aria-label') || '').includes(frag)) {
        if (!inp.checked) inp.click();
        return true;
      }
    }
    return false;
  }, ariaFragment);
  if (!ok) throw new Error(`Checkbox "${ariaFragment}" not found`);
}

async function waitForText(page, text, timeout = 20000) {
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

  await page.goto(`${baseUrl}/workflow`, { waitUntil: 'networkidle0', timeout: 20000 });
  const a11y = await page.evaluate(() => ({
    hasMain: Boolean(document.querySelector('main')),
    hasH1: Boolean(document.querySelector('h1')),
    unnamedButtons: Array.from(document.querySelectorAll('button')).filter(
      (b) => !b.textContent.trim() && !b.getAttribute('aria-label'),
    ).length,
  }));

  if (scenario === 'deterministic-multi') {
    await clickButton(page, 'Open session');
    await waitForText(page, 'Select source repositories');
    await typeIntoLabel(page, 'Source name', 'shop');
    await clickButton(page, 'Add source');
    await typeIntoLabel(page, 'Source name', 'shop2');
    await clickButton(page, 'Add source');
    await waitForText(page, 'local: shop');
    await waitForText(page, 'local: shop2');
    await clickButton(page, 'Create workspace');
    await waitForText(page, 'Scan and preview');
    await clickButton(page, 'Scan and preview');
    await waitForText(page, 'Omitted:');
    await waitForText(page, 'Select mode');
    await clickButton(page, 'Continue');
    await waitForText(page, 'Budget limits');
    await clickButton(page, 'Continue');
    await waitForText(page, 'Submit job');
    await clickButton(page, 'Submit job');
    await waitForText(page, 'Succeeded');
    await waitForText(page, 'View diagrams');
    const storageCheck = await page.evaluate(() => ({
      localLen: window.localStorage.length,
      sessionLen: window.sessionStorage.length,
      bodyLen: document.body.innerText.trim().length,
    }));
    return { ok: true, a11y, storageCheck, pageErrors, consoleErrors, text: await page.evaluate(() => document.body.innerText) };
  }

  if (scenario === 'architecture-canary-partial') {
    await clickButton(page, 'Open session');
    await waitForText(page, 'Select source repositories');
    await typeIntoLabel(page, 'Source name', 'shop');
    await clickButton(page, 'Create workspace');
    await waitForText(page, 'Scan and preview');
    await clickButton(page, 'Scan and preview');
    await waitForText(page, 'Select mode');
    await clickRadioByLabel(page, 'Architecture');
    await clickButton(page, 'Continue');
    await waitForText(page, 'Connect provider');
    await clickRadioByLabel(page, 'OpenAI');
    await typeIntoLabel(page, 'Model', 'gpt-4o-mini');
    await typeIntoLabel(page, 'API Key', CANARY);
    await clickButton(page, 'Connect provider');
    await waitForText(page, 'Provider connected');
    const leakCheck = await page.evaluate((canary) => ({
      localLen: window.localStorage.length,
      sessionLen: window.sessionStorage.length,
      inBody: document.body.innerText.includes(canary),
      inHref: window.location.href.includes(canary),
      inCookie: document.cookie.includes(canary),
    }), CANARY);
    await clickButton(page, 'Continue');
    await waitForText(page, 'Budget limits');
    await typeIntoLabel(page, 'Max AI requests', '1');
    await clickButton(page, 'Continue');
    await waitForText(page, 'Review and consent');
    await checkBoxByAria(page, 'external provider');
    await checkBoxByAria(page, 'source excerpts');
    await clickButton(page, 'Grant consent and continue');
    await waitForText(page, 'Submit job');
    await clickButton(page, 'Submit job');
    await waitForText(page, 'not current', 45000);
    return { ok: true, a11y, leakCheck, pageErrors, consoleErrors, text: await page.evaluate(() => document.body.innerText) };
  }

  if (scenario === 'cancel-mid-job') {
    await clickButton(page, 'Open session');
    await waitForText(page, 'Select source repositories');
    await typeIntoLabel(page, 'Source name', 'shop');
    await clickButton(page, 'Create workspace');
    await waitForText(page, 'Scan and preview');
    await clickButton(page, 'Scan and preview');
    await waitForText(page, 'Select mode');
    await clickRadioByLabel(page, 'Architecture');
    await clickButton(page, 'Continue');
    await waitForText(page, 'Connect provider');
    await typeIntoLabel(page, 'Model', 'replay-model');
    await clickButton(page, 'Connect provider');
    await waitForText(page, 'Provider connected');
    await clickButton(page, 'Continue');
    await waitForText(page, 'Budget limits');
    await clickButton(page, 'Continue');
    await waitForText(page, 'Submit job');
    await clickButton(page, 'Submit job');
    await waitForText(page, 'Cancel job');
    await clickButton(page, 'Cancel job');
    await waitForText(page, 'Cancelled', 25000);
    return { ok: true, a11y, pageErrors, consoleErrors, text: await page.evaluate(() => document.body.innerText) };
  }

  if (scenario === 'auth-token-session') {
    await typeIntoLabel(page, 'Auth token', 't'.repeat(40));
    await clickButton(page, 'Open session');
    await waitForText(page, 'Select source repositories');
    const storageCheck = await page.evaluate(() => ({
      localLen: window.localStorage.length,
      sessionLen: window.sessionStorage.length,
      inBody: document.body.innerText.includes('t'.repeat(40)),
      inHref: window.location.href.includes('t'.repeat(40)),
    }));
    return { ok: true, a11y, storageCheck, pageErrors, consoleErrors };
  }

  throw new Error(`Unknown scenario: ${scenario}`);
});

console.log(JSON.stringify(report));
