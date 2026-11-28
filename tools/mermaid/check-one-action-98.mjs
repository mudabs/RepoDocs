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

  await page.goto(`${baseUrl}/workflow`, { waitUntil: 'networkidle0' })
  await clickButton(page, 'Open session')

  await page.waitForSelector('input[aria-label="Quick GitHub repository URL"]', { timeout: 15000 })
  await page.type('input[aria-label="Quick GitHub repository URL"]', repoUrl)

  // First click without a provider opens inline provider setup
  await clickButton(page, 'Generate architecture')
  await page.waitForSelector('input[aria-label="Provider host URL"]', { timeout: 15000 })

  // Enter model and connect Ollama provider -> automatically completes workspace, scan, preview, submit, poll, and done!
  await page.evaluate(nextModel => {
    const inputs = Array.from(document.querySelectorAll('input'))
    const modelInput = inputs.find(i => i.placeholder?.includes('llama'))
    if (!modelInput) throw new Error('Model input not found')
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(modelInput, nextModel)
    modelInput.dispatchEvent(new Event('input', { bubbles: true }))
  }, 'replay-model')

  await clickButton(page, 'Connect provider')
  try {
    await clickButton(page, 'View diagrams')
  } catch (err) {
    const body = await page.evaluate(() => document.body.innerText)
    throw new Error(`Timed out waiting for View diagrams. Body: ${body}`)
  }
  try {
    await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 })
  } catch (err) {
    const body = await page.evaluate(() => document.body.innerText)
    throw new Error(`Timed out waiting for SVG. PageErrors: ${JSON.stringify(pageErrors)} Body: ${body}`)
  }

  // Back navigation must not trigger a second provider call
  await page.goBack({ waitUntil: 'networkidle0' })
  await page.waitForFunction(
    () => Array.from(document.querySelectorAll('button')).some(b => b.textContent?.trim() === 'View diagrams'),
    { timeout: 15000 },
  )

  assert(pageErrors.length === 0, `Uncaught page errors: ${pageErrors.join('; ')}`)
  console.log(JSON.stringify({ ok: true }))
} finally {
  await browser.close()
}
