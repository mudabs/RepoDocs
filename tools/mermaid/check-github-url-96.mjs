// Drives the built RepoDocs frontend through the GitHub URL entry (#96) in headless Chrome.
// Usage: node check-github-url-96.mjs <baseUrl> <browserPath> <scenario> [--no-sandbox]
import puppeteer from 'puppeteer';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [baseUrl, browserPath, scenario, flag] = process.argv.slice(2);
const URL_INPUT = 'input[aria-label="GitHub repository URL"]';

async function clickButton(page, text) {
  await page.waitForFunction(
    (target) => Array.from(document.querySelectorAll('button')).some((b) => !b.disabled && b.textContent.trim() === target),
    { timeout: 15000 },
    text,
  );
  await page.evaluate((target) => {
    Array.from(document.querySelectorAll('button')).find((b) => !b.disabled && b.textContent.trim() === target).click();
  }, text);
}

async function waitForText(page, text, timeout = 20000) {
  try {
    await page.waitForFunction((t) => document.body && document.body.innerText.includes(t), { timeout }, text);
  } catch (err) {
    const shown = await page.evaluate(() => document.body.innerText).catch(() => '<unreadable>');
    throw new Error(`Timed out waiting for "${text}". Page text:\n${shown.slice(-1500)}\n${err}`);
  }
}

async function chooseGitHub(page) {
  await page.evaluate(() => {
    const label = Array.from(document.querySelectorAll('label')).find((l) => l.textContent.includes('GitHub (pinned commit)'));
    label.querySelector('input[type="radio"]').click();
  });
  await page.waitForSelector(URL_INPUT, { timeout: 10000 });
}

async function replaceUrl(page, value) {
  await page.click(URL_INPUT, { clickCount: 3 });
  await page.keyboard.press('Backspace');
  await page.type(URL_INPUT, value);
}

const profile = await mkdtemp(join(tmpdir(), 'repodocs-gh-url-'));
const browser = await puppeteer.launch({
  executablePath: browserPath,
  headless: true,
  userDataDir: profile,
  args: flag === '--no-sandbox' ? ['--no-sandbox'] : [],
});
const pageErrors = [];
const externalRequests = [];
let report;
try {
  const page = await browser.newPage();
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('request', (req) => {
    if (!req.url().startsWith(baseUrl)) externalRequests.push(req.url());
  });
  await page.goto(baseUrl, { waitUntil: 'networkidle0', timeout: 20000 });
  await clickButton(page, 'Open session');
  await waitForText(page, 'Select source repositories');
  await chooseGitHub(page);

  if (scenario === 'url-to-preview') {
    // The URL alone: no owner, repository, SHA or workspace name is typed.
    await replaceUrl(page, 'https://github.com/mudabs/EventShare/');
    await clickButton(page, 'Create workspace');
    await clickButton(page, 'Scan and preview');
    await waitForText(page, 'Select mode');
    report = { ok: true, text: await page.evaluate(() => document.body.innerText) };
  } else if (scenario === 'stale-pin') {
    // A URL resolved earlier must not be used after the URL is changed to one that cannot be resolved.
    await replaceUrl(page, 'https://github.com/mudabs/EventShare');
    await clickButton(page, 'Resolve URL');
    await waitForText(page, 'Pinned');
    await replaceUrl(page, 'https://github.com/mudabs/Missing');
    await clickButton(page, 'Create workspace');
    await waitForText(page, 'not found');
    const hasScan = await page.evaluate(() =>
      Array.from(document.querySelectorAll('button')).some((b) => b.textContent.trim() === 'Scan and preview'));
    report = { ok: true, hasScan, text: await page.evaluate(() => document.body.innerText) };
  } else {
    throw new Error(`Unknown scenario: ${scenario}`);
  }
} finally {
  await browser.close().catch(() => {});
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
console.log(JSON.stringify({ ...report, pageErrors, externalRequests }));
