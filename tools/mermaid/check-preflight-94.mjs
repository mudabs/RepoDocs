import puppeteer from 'puppeteer'

const [mode, baseUrl, browserPath, sourceName] = process.argv.slice(2)

if (!mode || !baseUrl || !browserPath || !sourceName) {
  console.error('Usage: node check-preflight-94.mjs <blocked-context|budget-retry> <baseUrl> <browserPath> <sourceName>')
  process.exit(2)
}

function assert(cond, message) {
  if (!cond) {
    throw new Error(message)
  }
}

async function clickButton(page, text) {
  await page.waitForFunction(
    label => Array.from(document.querySelectorAll('button')).some(b => b.textContent?.trim() === label && !b.disabled),
    { timeout: 25000 },
    text,
  )
  await page.evaluate(label => {
    const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent?.trim() === label && !b.disabled)
    if (!btn) throw new Error(`Button not found or disabled: ${label}`)
    btn.click()
  }, text)
}

async function fillByAriaLabel(page, ariaLabel, value) {
  const selector = `[aria-label="${ariaLabel}"]`
  await page.waitForSelector(selector, { timeout: 15000 })
  await page.focus(selector)
  await page.click(selector, { clickCount: 3 })
  await page.type(selector, String(value))
}

async function setupToLimits(page, { providerKind, model, host, maxTokens }) {
  await page.goto(`${baseUrl}/workflow`, { waitUntil: 'networkidle0' })
  await clickButton(page, 'Open session')
  await fillByAriaLabel(page, 'Local source name', sourceName)
  await clickButton(page, 'Create workspace')
  await clickButton(page, 'Scan and preview')

  await page.waitForSelector('input[type="radio"]', { timeout: 15000 })
  await page.evaluate(() => {
    const labels = Array.from(document.querySelectorAll('label'))
    const arch = labels.find(l => l.textContent?.includes('Architecture'))
    const input = arch?.querySelector('input[type="radio"]')
    if (!input) throw new Error('Architecture radio not found')
    input.click()
  })
  await clickButton(page, 'Continue')

  await page.waitForSelector('[aria-label="Provider type"]', { timeout: 15000 })
  await page.evaluate(kind => {
    const group = document.querySelector('[aria-label="Provider type"]')
    const label = Array.from(group.querySelectorAll('label')).find(l => l.textContent?.toLowerCase().includes(kind))
    const input = label?.querySelector('input[type="radio"]')
    if (!input) throw new Error(`Provider radio not found for ${kind}`)
    input.click()
  }, providerKind)
  if (host) {
    await page.evaluate(nextHost => {
      const inputs = Array.from(document.querySelectorAll('input'))
      const hostInput = inputs.find(i => i.placeholder?.includes('127.0.0.1') || i.placeholder?.includes('openai.com'))
      if (hostInput) {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
        setter.call(hostInput, nextHost)
        hostInput.dispatchEvent(new Event('input', { bubbles: true }))
      }
    }, host)
  }
  await page.evaluate(nextModel => {
    const inputs = Array.from(document.querySelectorAll('input'))
    const modelInput = inputs.find(i => i.getAttribute('aria-label') === 'Model name' || i.placeholder?.includes('qwen') || i.placeholder?.includes('gpt'))
    if (!modelInput) throw new Error('Model input not found')
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(modelInput, nextModel)
    modelInput.dispatchEvent(new Event('input', { bubbles: true }))
  }, model)

  if (providerKind === 'openai') {
    await page.evaluate(secret => {
      const pw = document.querySelector('input[type="password"]')
      if (!pw) throw new Error('API key input not found')
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
      setter.call(pw, secret)
      pw.dispatchEvent(new Event('input', { bubbles: true }))
    }, 'sk-synthetic-94-canary-key')
  }

  await clickButton(page, 'Connect provider')
  await clickButton(page, 'Continue')

  if (maxTokens !== undefined) {
    await fillByAriaLabel(page, 'Maximum total tokens', maxTokens)
  }
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

  if (mode === 'blocked-context') {
    await setupToLimits(page, {
      providerKind: 'openai',
      model: 'gpt-6-luna',
      host: 'http://127.0.0.1:9',
    })
    const region = await page.waitForSelector('[aria-label="Per-request model capacity and preflight"]', { timeout: 15000 })
    const regionText = await page.evaluate(el => el.textContent || '', region)
    assert(regionText.includes('4,096'), `Expected 4,096 context window, got: ${regionText}`)
    assert(regionText.includes('fallback default'), `Expected fallback provenance, got: ${regionText}`)
    assert(regionText.includes('byte-bound'), `Expected byte-bound estimate method, got: ${regionText}`)

    const alertText = await page.evaluate(el => el.querySelector('[role="alert"]')?.textContent || '', region)
    assert(alertText.includes('exceeding the configured 4096-token context window'), `Unexpected alert: ${alertText}`)

    const continueDisabled = await page.evaluate(() => {
      const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent?.trim() === 'Continue')
      return Boolean(btn && btn.disabled)
    })
    assert(continueDisabled, 'Continue button must be disabled when preflight fails')

    const changeSourcesVisible = await page.evaluate(() =>
      Array.from(document.querySelectorAll('button')).some(b => b.textContent?.trim() === 'Change sources'),
    )
    assert(changeSourcesVisible, 'Change sources button should be visible')
  } else if (mode === 'budget-retry') {
    await setupToLimits(page, {
      providerKind: 'ollama',
      model: 'qwen2.5-coder',
      host: 'http://127.0.0.1:11434',
      maxTokens: '500',
    })
    await clickButton(page, 'Continue')
    await clickButton(page, 'Submit job')

    await page.waitForFunction(
      () => Array.from(document.querySelectorAll('button')).some(b => b.textContent?.trim() === 'Retry with current sources'),
      { timeout: 30000 },
    )
    const mainText = await page.evaluate(() => document.querySelector('main')?.textContent || '')
    assert(mainText.includes('budget-exhausted'), `Expected budget-exhausted in done card, got: ${mainText}`)
    assert(mainText.includes('Generation requests:0'), `Expected Generation requests:0 in done card, got: ${mainText}`)

    await clickButton(page, 'Retry with current sources')
    await fillByAriaLabel(page, 'Maximum total tokens', '100000')
    await clickButton(page, 'Continue')
    await clickButton(page, 'Submit job')

    try {
      await page.waitForFunction(
        () => Array.from(document.querySelectorAll('button')).some(b => b.textContent?.trim() === 'View diagrams'),
        { timeout: 30000 },
      )
    } catch (err) {
      const text = await page.evaluate(() => document.querySelector('main')?.textContent || '')
      throw new Error(`Timed out waiting for View diagrams. Main text: ${text}`)
    }
  } else {
    throw new Error(`Unknown mode: ${mode}`)
  }

  assert(pageErrors.length === 0, `Uncaught page errors: ${pageErrors.join('; ')}`)
  console.log(JSON.stringify({ ok: true, mode }))
} finally {
  await browser.close()
}
