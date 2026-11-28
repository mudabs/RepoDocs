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
    const attackerUrl = extraArg;
    // Step 1: On the RepoDocs origin, establish the victim session, workspace, provider,
    // and two successful positive-control jobs (deterministic + provider-backed) in this same browser session.
    await page.goto(`${baseUrl}/workflow`, { waitUntil: 'networkidle0', timeout: 20000 });
    const victimIds = await page.evaluate(async () => {
      async function api(method, path, body, csrf) {
        const headers = { 'Content-Type': 'application/json' };
        if (csrf) headers['x-csrf-token'] = csrf;
        const resp = await fetch(path, {
          method,
          credentials: 'include',
          headers,
          body: body !== undefined ? JSON.stringify(body) : undefined,
        });
        if (!resp.ok) throw new Error(`Victim setup ${method} ${path} failed: ${resp.status}`);
        return resp.json();
      }
      async function waitJob(jobId) {
        for (let i = 0; i < 200; i += 1) {
          const j = await api('GET', `/api/v1/jobs/${jobId}`);
          if (j.state === 'succeeded') return j;
          if (['failed', 'cancelled', 'interrupted'].includes(j.state)) {
            throw new Error(`Positive control job ${jobId} ended in ${j.state}`);
          }
          await new Promise((r) => setTimeout(r, 50));
        }
        throw new Error(`Timed out waiting for ${jobId}`);
      }

      const { csrf_token: csrf } = await api('POST', '/api/v1/session', {});
      const ws = await api('POST', '/api/v1/workspaces', { name: 'Shop', sources: [{ kind: 'local', name: 'shop' }] }, csrf);
      const prov = await api('POST', '/api/v1/providers', { provider: 'ollama', host: 'http://127.0.0.1:11434', model: 'replay-model' }, csrf);
      const pre = await api('POST', `/api/v1/workspaces/${ws.workspace_id}/preview`, { provider_session_id: prov.provider_session_id }, csrf);

      const detBody = {
        contract_version: '1.0',
        kind: 'request',
        workspace_id: ws.workspace_id,
        idempotency_key: 'pos-det-1',
        mode: 'deterministic',
        provider_session_id: null,
        context_hash: null,
        consent_receipt_id: null,
        views: ['overview', 'erd'],
        limits: { max_requests: 0, max_total_tokens: null, max_cost_microunits: 0, currency: 'USD' },
      };
      const detJob = await api('POST', '/api/v1/jobs', detBody, csrf);
      await waitJob(detJob.job_id);

      const archBody = {
        contract_version: '1.0',
        kind: 'request',
        workspace_id: ws.workspace_id,
        idempotency_key: 'pos-arch-1',
        mode: 'architecture',
        provider_session_id: prov.provider_session_id,
        context_hash: pre.context_hash,
        consent_receipt_id: null,
        views: ['overview', 'components'],
        limits: { max_requests: 3, max_total_tokens: 100000, max_cost_microunits: null, currency: null },
      };
      const archJob = await api('POST', '/api/v1/jobs', archBody, csrf);
      await waitJob(archJob.job_id);

      // Return ONLY public IDs/hashes; CSRF token and cookies stay out of attacker JS.
      return {
        workspaceId: ws.workspace_id,
        providerSessionId: prov.provider_session_id,
        contextHash: pre.context_hash,
        jobId: archJob.job_id,
      };
    });

    // Step 2: Navigate the SAME browser session (holding the victim cookie) to the unrelated attacker origin.
    // Attacker JS receives NO victim CSRF token or API key.
    await page.goto(attackerUrl, { waitUntil: 'networkidle0', timeout: 20000 });
    const attackResults = await page.evaluate(async (targetBase, ids) => {
      const detAttack = {
        contract_version: '1.0',
        kind: 'request',
        workspace_id: ids.workspaceId,
        idempotency_key: 'atk-det-preflight-1',
        mode: 'deterministic',
        provider_session_id: null,
        context_hash: null,
        consent_receipt_id: null,
        views: ['overview'],
        limits: { max_requests: 0, max_total_tokens: null, max_cost_microunits: 0, currency: 'USD' },
      };
      const archAttack = {
        contract_version: '1.0',
        kind: 'request',
        workspace_id: ids.workspaceId,
        idempotency_key: 'atk-arch-simple-1',
        mode: 'architecture',
        provider_session_id: ids.providerSessionId,
        context_hash: ids.contextHash,
        consent_receipt_id: null,
        views: ['overview', 'components'],
        limits: { max_requests: 3, max_total_tokens: 100000, max_cost_microunits: null, currency: null },
      };
      const noCorsAttack = { ...detAttack, idempotency_key: 'atk-det-nocors-1' };

      const results = {};
      try {
        const resp = await fetch(`${targetBase}/api/v1/jobs`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(detAttack),
        });
        results.preflightedJob = { readable: true, status: resp.status };
      } catch (err) {
        results.preflightedJob = { readable: false, error: String(err) };
      }

      try {
        const resp = await fetch(`${targetBase}/api/v1/jobs`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify(archAttack),
        });
        results.simpleCorsJob = { readable: true, status: resp.status };
      } catch (err) {
        results.simpleCorsJob = { readable: false, error: String(err) };
      }

      try {
        const resp = await fetch(`${targetBase}/api/v1/jobs`, {
          method: 'POST',
          mode: 'no-cors',
          credentials: 'include',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify(noCorsAttack),
        });
        results.noCorsJob = { type: resp.type, status: resp.status, ok: resp.ok };
      } catch (err) {
        results.noCorsJob = { error: String(err) };
      }

      try {
        const resp = await fetch(`${targetBase}/api/v1/jobs/${ids.jobId}`, {
          method: 'GET',
          credentials: 'include',
        });
        results.readJob = { readable: true, status: resp.status };
      } catch (err) {
        results.readJob = { readable: false, error: String(err) };
      }

      return results;
    }, baseUrl, victimIds);

    return { ok: true, victimIds, attackResults, dialogs, pageErrors, externalRequests };
  }

  if (scenario === 'hostile-pinned-viewer') {
    await page.goto(`${baseUrl}/workflow`, { waitUntil: 'networkidle0', timeout: 20000 });
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
