// Loads a RepoDocs architecture export in a real browser and reports what happened, as JSON.
// Optional tooling: used by the test suite only. Usage: node check-export.mjs <file> <browser> [--no-sandbox|--] [screenshot.png]
import puppeteer from 'puppeteer';
import { pathToFileURL } from 'node:url';

const [file, browser, flag, screenshot] = process.argv.slice(2);
const report = { dialogs: [], console: [], violations: [], requests: [], clicked: null, alternative: null, zoomed: false, drawn: false, links: [] };
const instance = await puppeteer.launch({ executablePath: browser, headless: true, args: flag === '--no-sandbox' ? ['--no-sandbox'] : [] });
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
}
console.log(JSON.stringify(report));
