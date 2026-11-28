import puppeteer from 'puppeteer'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const [baseUrl, browserPath, sourceName] = process.argv.slice(2)
if (!baseUrl || !browserPath || !sourceName) {
  console.error('Usage: node check-nav-97.mjs <baseUrl> <browserPath> <sourceName>')
  process.exit(2)
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg)
}

async function clickButton(page, text) {
  await page.waitForFunction(
    label => Array.from(document.querySelectorAll('button')).some(b => b.textContent?.trim() === label && !b.disabled),
    { timeout: 25000 },
    text,
  )
  await page.evaluate(label => {
    const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent?.trim() === label && !b.disabled)
    if (!btn) throw new Error(`Button not found: ${label}`)
    btn.click()
  }, text)
}

// A persistent profile, so a browser restart keeps the session cookie the way a real browser does.
const profile = await mkdtemp(join(tmpdir(), 'repodocs-nav-97-'))
const launch = () => puppeteer.launch({
  executablePath: browserPath,
  headless: true,
  userDataDir: profile,
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
})
let browser = await launch()

const pageErrors = []

try {
  const page = await browser.newPage()
  page.on('pageerror', err => pageErrors.push(err.message))

  await page.goto(`${baseUrl}/workflow`, { waitUntil: 'networkidle0' })
  await clickButton(page, 'Open session')
  await page.waitForSelector('input[aria-label="Local source name"]', { timeout: 15000 })
  await page.type('input[aria-label="Local source name"]', sourceName)
  await clickButton(page, 'Create workspace')
  await clickButton(page, 'Scan and preview')
  await clickButton(page, 'Continue')
  await clickButton(page, 'Continue')
  await clickButton(page, 'Submit job')
  try {
    await clickButton(page, 'View diagrams')
  } catch (err) {
    const body = await page.evaluate(() => document.body.innerText)
    throw new Error(`Timed out waiting for View diagrams. Body: ${body}`)
  }

  await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 })
  const viewerUrl = page.url()
  assert(viewerUrl.includes('/viewer/job_'), `Expected /viewer/job_ URL, got ${viewerUrl}`)

  // 1. Browser Back restores the workflow result without starting another job
  await page.goBack({ waitUntil: 'networkidle0' })
  await page.waitForFunction(
    () => Array.from(document.querySelectorAll('button')).some(b => b.textContent?.trim() === 'View diagrams'),
    { timeout: 15000 },
  )
  const backUrl = page.url()
  assert(backUrl.includes('/workflow/draft_'), `Expected /workflow/draft_ URL on Back, got ${backUrl}`)

  // 2. Browser Forward returns to the viewer
  await page.goForward({ waitUntil: 'networkidle0' })
  await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 15000 })

  // 3. Page reload on bookmarked /viewer/:jobId reconnects and renders without extra inference
  await page.reload({ waitUntil: 'networkidle0' })
  await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 15000 })

  // 4. Reload on /workflow/:draftId restores saved draft state
  await page.goto(backUrl, { waitUntil: 'networkidle0' })
  await page.waitForFunction(
    () => Array.from(document.querySelectorAll('button')).some(b => b.textContent?.trim() === 'View diagrams'),
    { timeout: 15000 },
  )

  // Verify no secrets/tokens in localStorage or sessionStorage
  const storage = await page.evaluate(() => ({
    localLen: window.localStorage.length,
    sessionLen: window.sessionStorage.length,
  }))
  assert(storage.localLen === 0 && storage.sessionLen === 0, `Unexpected browser storage: ${JSON.stringify(storage)}`)
  assert(pageErrors.length === 0, `Uncaught page errors: ${pageErrors.join('; ')}`)

  // 5. Browser restart: a new browser process with the same profile reopens the bookmarked result.
  await browser.close()
  browser = await launch()
  const restarted = await browser.newPage()
  restarted.on('pageerror', err => pageErrors.push(err.message))
  await restarted.goto(viewerUrl, { waitUntil: 'networkidle0' })
  await restarted.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 })
  assert(pageErrors.length === 0, `Uncaught page errors after browser restart: ${pageErrors.join('; ')}`)

  console.log(JSON.stringify({ ok: true, viewerUrl, backUrl }))
} finally {
  await browser.close().catch(() => {})
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
}
