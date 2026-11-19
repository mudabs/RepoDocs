// Loads a RepoDocs architecture export in a real browser and reports what happened, as JSON.
// Optional tooling: used by the test suite only. Usage: node check-export.mjs <file> <browser> [--no-sandbox|--] [screenshot.png]
//
// Every launch gets its own new profile directory, which this script removes afterwards. A launch that fails is
// tried once more with another new profile (LAUNCH_ATTEMPTS in all) and every failure is reported, so a retry never
// hides what happened. On Windows Puppeteer words ANY failed launch as "already running" when the profile holds a
// lockfile, which a profile this script created a moment ago always does; the note below says so.
import puppeteer from 'puppeteer';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const LAUNCH_ATTEMPTS = 2;

const [file, browser, flag, screenshot] = process.argv.slice(2);
const report = { dialogs: [], console: [], violations: [], requests: [], clicked: null, alternative: null, zoomed: false, drawn: false, links: [] };
async function launch() {
  const failures = [];
  for (let attempt = 1; attempt <= LAUNCH_ATTEMPTS; attempt += 1) {
    const profile = await mkdtemp(join(tmpdir(), 'repodocs-export-profile-'));
    try {
      const instance = await puppeteer.launch({ executablePath: browser, headless: true, userDataDir: profile, args: flag === '--no-sandbox' ? ['--no-sandbox'] : [] });
      return { instance, profile, failures };
    } catch (error) {
      const message = String(error && error.message || error).split('\n')[0];
      failures.push({ attempt, message, note: /already running/.test(message) ? 'the profile was created for this launch, so the browser did not start; the real cause was not reported by Puppeteer' : undefined });
      await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  }
  console.error(JSON.stringify({ launchFailed: true, attempts: failures }));
  process.exit(1);
}

const { instance, profile, failures: launchFailures } = await launch();
report.launchFailures = launchFailures;
try {
  const page = await instance.newPage();
  await page.setViewport({ width: 1400, height: 900 });
  page.on('console', (message) => report.console.push(message.type() + ': ' + message.text()));
  page.on('dialog', (dialog) => { report.dialogs.push(dialog.message()); dialog.dismiss(); });
  page.on('pageerror', (error) => report.console.push('pageerror: ' + error.message));
  page.on('request', (request) => { if (!request.url().startsWith('file:') && !request.url().startsWith('data:')) { report.requests.push(request.url()); } });
  await page.evaluateOnNewDocument(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      (window.__violations = window.__violations || []).push(event.violatedDirective + ' ' + event.blockedURI);
    });
  });
  await page.goto(pathToFileURL(file).href, { waitUntil: 'load' });
  await page.waitForSelector('#canvas svg', { timeout: 60000 });
  report.drawn = true;
  const before = await page.$eval('#canvas', (node) => node.style.transform);
  await page.click('#in');
  const after = await page.$eval('#canvas', (node) => node.style.transform);
  report.zoomed = before !== after;
  const clickable = await page.$('#canvas g.node.clickable');
  if (clickable) {
    await clickable.click();
    report.clicked = await page.$eval('#details', (node) => node.innerText);
  }
  report.links = await page.$$eval('#details a', (anchors) => anchors.map((anchor) => anchor.href));
  const alternative = await page.$('#alternatives button');
  if (alternative) {
    await alternative.click();
    report.alternative = await page.$eval('#details', (node) => node.innerText);
  }
  report.violations = await page.evaluate(() => window.__violations || []);
  report.scripts = await page.$$eval('script', (nodes) => nodes.length);
  if (screenshot) { await page.screenshot({ path: screenshot }); }
} finally {
  await instance.close();
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
console.log(JSON.stringify(report));
