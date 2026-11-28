import puppeteer from 'puppeteer'

const [baseUrl, browserPath, repoUrl] = process.argv.slice(2)
if (!baseUrl || !browserPath || !repoUrl) {
  console.error('Usage: node check-one-action-98.mjs <baseUrl> <browserPath> <repoUrl>')
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

const browser = await puppeteer.launch({
  executablePath: browserPath,
  headless: true,
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
})

const pageErrors = []

try {
  const page = await browser.newPage()
  page.on('pageerror', err => pageErrors.push(err.message))

  // The URL-first home (#99) is the one primary action: URL, Generate, first-use provider setup, result.
  await page.goto(`${baseUrl}/`, { waitUntil: 'networkidle0' })
  await page.waitForSelector('#repo-url', { timeout: 15000 })
  await page.type('#repo-url', repoUrl)

  // First click without a provider opens provider setup
  await clickButton(page, 'Generate architecture')
  await page.waitForSelector('input[aria-label="Provider host URL"]', { timeout: 15000 })
  await page.evaluate(nextModel => {
    const modelInput = Array.from(document.querySelectorAll('input')).find(i => i.getAttribute('aria-label') === 'Model name')
    if (!modelInput) throw new Error('Model input not found')
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(modelInput, nextModel)
    modelInput.dispatchEvent(new Event('input', { bubbles: true }))
  }, 'replay-model')

  // Connecting continues the same generation: resolve, workspace, scan, submit, progress, result.
  await clickButton(page, 'Connect provider')
  try {
    await clickButton(page, 'View diagrams')
  } catch (err) {
    const body = await page.evaluate(() => document.body.innerText)
    throw new Error(`Timed out waiting for View diagrams. Body: ${body}`)
  }
  await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 })

  // Back returns to the home page with the saved project; reopening it must not run the model again.
  await page.goBack({ waitUntil: 'networkidle0' })
  await page.waitForSelector('button[aria-label="Open result for mudabs/EventShare"]', { timeout: 15000 })
  const historyText = await page.evaluate(() => document.querySelector('[aria-label="Saved projects"]').innerText)
  assert(/github\.com\/mudabs\/EventShare @ [0-9a-f]{7}/.test(historyText) && !historyText.includes('undefined'), `Saved project source is not shown: ${historyText}`)
  await page.click('button[aria-label="Open result for mudabs/EventShare"]')
  await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 })

  assert(pageErrors.length === 0, `Uncaught page errors: ${pageErrors.join('; ')}`)
  console.log(JSON.stringify({ ok: true }))
} finally {
  await browser.close()
}
