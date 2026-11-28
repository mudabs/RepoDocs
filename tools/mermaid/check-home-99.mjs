// Real-browser acceptance for the URL-first home (#99).
// Usage: node check-home-99.mjs <baseUrl> <browserPath> <scenario> [--no-sandbox]
import puppeteer from 'puppeteer';

const [baseUrl, browserPath, scenario, flag] = process.argv.slice(2);
const CANARY = 'sk-ant-canary-0123456789abcdefghijklmnopqrstuv';

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function waitForText(page, text, timeout = 30000) {
  try {
    await page.waitForFunction((t) => document.body && document.body.innerText.includes(t), { timeout }, text);
  } catch (err) {
    const shown = await page.evaluate(() => document.body.innerText).catch(() => '');
    throw new Error(`Timed out waiting for "${text}". Page text:\n${shown.slice(-1500)}`);
  }
}

async function clickButton(page, text, timeout = 30000) {
  await page.waitForFunction(
    (t) => Array.from(document.querySelectorAll('button')).some((b) => !b.disabled && b.textContent.trim() === t),
    { timeout }, text);
  await page.evaluate((t) => Array.from(document.querySelectorAll('button')).find((b) => !b.disabled && b.textContent.trim() === t).click(), text);
}

async function tabUntil(page, predicateSource, maxTabs = 30) {
  for (let i = 0; i < maxTabs; i += 1) {
    await page.keyboard.press('Tab');
    const hit = await page.evaluate(predicateSource);
    if (hit) return i + 1;
  }
  throw new Error(`Keyboard focus never reached the target: ${predicateSource}`);
}

async function noHorizontalOverflow(page, width) {
  await page.setViewport({ width, height: 800 });
  await new Promise((r) => setTimeout(r, 250));
  const m = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  assert(m.scroll <= m.client + 1, `Horizontal overflow at ${width}px: ${JSON.stringify(m)}`);
  return m;
}

// WCAG relative-luminance contrast between an element's text colour and the first opaque background behind it.
const CONTRAST = `(selector) => {
  const parse = (c) => (c.match(/[\\d.]+/g) || []).map(Number);
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!el.textContent.trim() || el.offsetParent === null) continue;
    const fg = parse(getComputedStyle(el).color);
    let bgEl = el, bg = [255, 255, 255, 0];
    while (bgEl) {
      const c = parse(getComputedStyle(bgEl).backgroundColor);
      if (c.length >= 3 && (c.length === 3 || c[3] > 0.9)) { bg = c; break; }
      bgEl = bgEl.parentElement;
    }
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    out.push({ text: el.textContent.trim().slice(0, 30), ratio: Math.round(((a + 0.05) / (b + 0.05)) * 100) / 100 });
  }
  return out;
}`;

const browser = await puppeteer.launch({
  executablePath: browserPath,
  headless: true,
  args: flag === '--no-sandbox' ? ['--no-sandbox'] : [],
});
const pageErrors = [];
const consoleErrors = [];

try {
  const page = await browser.newPage();
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
  await page.setViewport({ width: 1024, height: 860 });
  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

  await page.goto(`${baseUrl}/`, { waitUntil: 'networkidle0' });
  await waitForText(page, 'Generate architecture diagrams');
  // Collect every live-region announcement.
  await page.evaluate(() => {
    window.__announcements = [];
    const region = document.querySelector('[role="status"][aria-live="polite"]');
    new MutationObserver(() => window.__announcements.push(region.textContent)).observe(region, { childList: true, characterData: true, subtree: true });
  });

  if (scenario === 'deterministic-keyboard') {
    const tabsToUrl = await tabUntil(page, () => document.activeElement && document.activeElement.id === 'repo-url');
    await page.keyboard.type('shop');
    await tabUntil(page, () => document.activeElement && document.activeElement.type === 'radio');
    await page.keyboard.press('ArrowDown');
    const localChosen = await page.evaluate(() => document.activeElement.checked && document.activeElement.closest('label').textContent.includes('Local facts only'));
    assert(localChosen, 'Arrow key did not select Local facts only');
    await page.keyboard.press('Enter');
    await waitForText(page, 'Diagrams are ready.');
    const focusAfter = await page.evaluate(() => document.activeElement && document.activeElement.textContent.trim());
    assert(focusAfter === 'Result', `Focus did not move to the Result heading: ${focusAfter}`);
    const announcements = await page.evaluate(() => window.__announcements);
    assert(announcements.some((a) => a.includes('Diagrams are ready')), `Completion was not announced: ${JSON.stringify(announcements)}`);
    assert(announcements.some((a) => a.includes('Scan and prepare context')), `Preparation was not announced: ${JSON.stringify(announcements)}`);
    const usage = await page.evaluate(() => document.body.innerText.includes('No model request was made.'));
    assert(usage, 'Local run did not state that no model request was made');

    await tabUntil(page, () => document.activeElement && document.activeElement.textContent.trim() === 'View diagrams');
    await page.keyboard.press('Enter');
    await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 });
    await new Promise((r) => setTimeout(r, 500));
    const viewerUrl = page.url();
    assert(viewerUrl.includes('/viewer/job_'), `Expected a viewer deep link, got ${viewerUrl}`);
    const diagram = await page.evaluate(() => {
      const svg = document.querySelector('[aria-label^="Diagram canvas for"] svg');
      const rects = Array.from(svg.querySelectorAll('g.node rect.label-container, g.node rect.basic'));
      const texts = Array.from(svg.querySelectorAll('g.node text')).filter((t) => t.textContent.trim());
      const vb = svg.viewBox.baseVal;
      const content = svg.querySelector(':scope > g').getBBox();
      return {
        fills: rects.map((r) => getComputedStyle(r).fill),
        heights: texts.map((t) => t.getBoundingClientRect().height),
        clipped: content.y + content.height > vb.y + vb.height + 1 || content.x + content.width > vb.x + vb.width + 1,
      };
    });
    assert(diagram.fills.length > 0 && diagram.fills.every((f) => f !== 'rgb(0, 0, 0)' && f !== 'none'), `Unreadable fills ${JSON.stringify(diagram.fills)}`);
    assert(diagram.heights.length > 0 && diagram.heights.every((h) => h >= 9), `Labels too small ${JSON.stringify(diagram.heights)}`);
    assert(!diagram.clipped, 'The diagram content extends past its viewBox');
    // Zoom must take effect at once with reduced motion on (no transition left pending).
    const svgWidth = () => page.evaluate(() => document.querySelector('[aria-label^="Diagram canvas for"] svg').getBoundingClientRect().width);
    const beforeZoom = await svgWidth();
    await clickButton(page, 'Zoom +');
    const afterZoom = await svgWidth();
    assert(afterZoom > beforeZoom * 1.1, `Zoom + did not enlarge the diagram (${beforeZoom} -> ${afterZoom})`);
    const viewerNarrow = await noHorizontalOverflow(page, 375);
    await page.setViewport({ width: 1280, height: 860 });

    // Reload the deep link: the result renders again without any new job.
    await page.reload({ waitUntil: 'networkidle0' });
    await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 });

    // Browser Back returns to the home page with the saved project listed.
    await page.goBack({ waitUntil: 'networkidle0' });
    await waitForText(page, 'Saved projects');
    await page.waitForSelector('button[aria-label="Open result for shop"]', { timeout: 15000 });
    const repoValue = await page.$eval('#repo-url', (el) => el.value);
    assert(repoValue === 'shop', `Repository field was not restored: ${repoValue}`);

    const contrast = await page.evaluate(`(${CONTRAST})('.rd-muted, .rd-label, .rd-btn, .rd-history-name, h1, h2')`);
    const low = contrast.filter((c) => c.ratio < 4.5);
    assert(low.length === 0, `Low contrast text: ${JSON.stringify(low)}`);
    const homeNarrow = await noHorizontalOverflow(page, 320);
    await page.setViewport({ width: 1280, height: 860 });

    // Reopen from history, then delete with an explicit confirmation.
    await page.click('button[aria-label="Open result for shop"]');
    await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 });
    await page.goBack({ waitUntil: 'networkidle0' });
    await page.waitForSelector('button[aria-label="Delete shop"]', { timeout: 15000 });
    await page.click('button[aria-label="Delete shop"]');
    await page.waitForSelector('button[aria-label="Permanently delete shop"]', { timeout: 5000 });
    await page.click('button[aria-label="Permanently delete shop"]');
    await waitForText(page, 'Nothing saved yet.');

    const storage = await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }));
    assert(storage.local === 0 && storage.session === 0, `Browser storage used: ${JSON.stringify(storage)}`);
    assert(pageErrors.length === 0, `Page errors: ${pageErrors.join('; ')}`);
    assert(consoleErrors.length === 0, `Console errors: ${consoleErrors.join('; ')}`);
    console.log(JSON.stringify({ ok: true, tabsToUrl, viewerUrl, viewerNarrow, homeNarrow, contrastChecked: contrast.length }));
  } else if (scenario === 'architecture-review') {
    await page.type('#repo-url', 'shop');
    await clickButton(page, 'Generate architecture');
    await waitForText(page, 'Connect an AI provider');
    await page.evaluate(() => {
      const radio = Array.from(document.querySelectorAll('input[type="radio"]')).find((r) => r.closest('label') && r.closest('label').textContent.includes('OpenAI'));
      radio.click();
    });
    await page.type('input[aria-label="Model name"]', 'gpt-4o-mini');
    await page.type('input[type="password"]', CANARY);
    await clickButton(page, 'Connect provider');
    await waitForText(page, 'Review what will be sent');
    const sendDisabled = await page.evaluate(() => Array.from(document.querySelectorAll('button')).find((b) => b.textContent.trim() === 'Send and generate').disabled);
    assert(sendDisabled, 'Send and generate was enabled before consent');
    const reviewText = await page.evaluate(() => document.body.innerText);
    assert(reviewText.includes('Nothing has been sent yet.'), 'Review did not say nothing was sent yet');
    await page.click('input[aria-label="I consent to sending data to an external provider"]');
    await page.click('input[aria-label="I consent to sharing source excerpts"]');
    await clickButton(page, 'Send and generate');
    await page.waitForFunction(() => /Some diagrams were produced|stopped at a usage limit|Diagrams are ready|run failed/.test(document.body.innerText), { timeout: 45000 });
    const outcome = await page.evaluate(() => document.body.innerText);
    const leak = await page.evaluate((c) => ({
      local: localStorage.length, session: sessionStorage.length,
      inBody: document.body.innerText.includes(c), inHref: location.href.includes(c), inCookie: document.cookie.includes(c),
    }), CANARY);

    // Reopening from history must not run the model; a second Generate needs a fresh consent review.
    await page.reload({ waitUntil: 'networkidle0' });
    await page.waitForSelector('button[aria-label="Open result for shop"], button[aria-label="Delete shop"]', { timeout: 15000 });
    const openButton = await page.$('button[aria-label="Open result for shop"]');
    if (openButton) {
      await openButton.click();
      await page.waitForSelector('[aria-label^="Diagram canvas for"]', { timeout: 20000 });
      await page.goBack({ waitUntil: 'networkidle0' });
      await waitForText(page, 'Saved projects');
    }
    await page.$eval('#repo-url', (el) => { el.value = ''; });
    await page.type('#repo-url', 'shop');
    await clickButton(page, 'Generate architecture');
    await waitForText(page, 'Connect an AI provider');
    assert(pageErrors.length === 0, `Page errors: ${pageErrors.join('; ')}`);
    assert(consoleErrors.length === 0, `Console errors: ${consoleErrors.join('; ')}`);
    console.log(JSON.stringify({ ok: true, outcome: outcome.slice(0, 1500), leak, reopened: Boolean(openButton) }));
  } else if (scenario === 'reconnect-cancel' || scenario === 'provider-error') {
    const connectOllama = async () => {
      await waitForText(page, 'Connect an AI provider');
      await page.type('input[aria-label="Model name"]', 'replay-model');
      await clickButton(page, 'Connect provider');
    };
    await page.type('#repo-url', 'shop');
    await clickButton(page, 'Generate architecture');
    await connectOllama();
    if (scenario === 'provider-error') {
      await waitForText(page, 'The run failed and produced no current result.', 45000);
      await page.waitForFunction(() => Array.from(document.querySelectorAll('button')).some((b) => b.textContent.trim() === 'Start another'));
      assert(pageErrors.length === 0, `Page errors: ${pageErrors.join('; ')}`);
      console.log(JSON.stringify({ ok: true, text: (await page.evaluate(() => document.body.innerText)).slice(0, 1200) }));
    } else {
      await waitForText(page, 'Generating');
      // Reload mid-run: the page forgets the provider, but the run continues on the server.
      await page.reload({ waitUntil: 'networkidle0' });
      await page.waitForSelector('button[aria-label="Show progress for shop"]', { timeout: 15000 });
      await page.evaluate(() => {
        window.__announcements = [];
        const region = document.querySelector('[role="status"][aria-live="polite"]');
        new MutationObserver(() => window.__announcements.push(region.textContent)).observe(region, { childList: true, characterData: true, subtree: true });
      });
      // Generating again for the same source follows the active run instead of submitting a second one.
      await page.$eval('#repo-url', (el) => { el.value = ''; });
      await page.type('#repo-url', 'shop');
      await clickButton(page, 'Generate architecture');
      await connectOllama();
      await waitForText(page, 'Generating');
      await clickButton(page, 'Cancel generation');
      await waitForText(page, 'The run was cancelled.', 45000);
      const announcements = await page.evaluate(() => window.__announcements);
      assert(announcements.some((a) => a.includes('already in progress')), `Reconnect was not announced: ${JSON.stringify(announcements)}`);
      assert(pageErrors.length === 0, `Page errors: ${pageErrors.join('; ')}`);
      console.log(JSON.stringify({ ok: true }));
    }
  } else if (scenario === 'model-picker') {
    await page.type('#repo-url', 'shop');
    await clickButton(page, 'Generate architecture');
    await waitForText(page, 'Connect an AI provider');
    const options = () => page.$$eval('select[aria-label="Available models"] option', (items) => items.slice(1).map((o) => o.textContent.trim()));
    await page.waitForSelector('select[aria-label="Available models"]', { timeout: 10000 });
    const ollamaOptions = await options();
    await page.evaluate(() => {
      Array.from(document.querySelectorAll('input[type="radio"]')).find((r) => r.closest('label') && r.closest('label').textContent.includes('OpenAI')).click();
    });
    await page.waitForFunction(() => Array.from(document.querySelectorAll('select[aria-label="Available models"] option')).some((o) => o.value === 'gpt-6-sol'));
    const openaiOptions = await options();
    await page.select('select[aria-label="Available models"]', 'gpt-6-sol');
    const chosen = await page.$eval('input[aria-label="Model name"]', (el) => el.value);
    assert(pageErrors.length === 0, `Page errors: ${pageErrors.join('; ')}`);
    console.log(JSON.stringify({ ok: true, ollamaOptions, openaiOptions, chosen }));
  } else {
    throw new Error(`Unknown scenario ${scenario}`);
  }
} finally {
  await browser.close();
}
