import puppeteer from 'puppeteer'

const [baseUrl, browserPath, sourceName] = process.argv.slice(2)
if (!baseUrl || !browserPath || !sourceName) {
  console.error('Usage: node check-mermaid-93.mjs <baseUrl> <browserPath> <sourceName>')
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
const consoleErrors = []
const externalRequests = []

try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1280, height: 800 })
  page.on('pageerror', err => pageErrors.push(err.message))
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })
  await page.setRequestInterception(true)
  page.on('request', req => {
    const url = req.url()
    if (!url.startsWith(baseUrl) && !url.startsWith('data:') && !url.startsWith('blob:')) {
      externalRequests.push(url)
    }
    req.continue()
  })

  await page.goto(`${baseUrl}/workflow`, { waitUntil: 'networkidle0' })
  await clickButton(page, 'Open session')
  await page.waitForSelector('input[aria-label="Local source name"]', { timeout: 15000 })
  await page.type('input[aria-label="Local source name"]', sourceName)
  await clickButton(page, 'Create workspace')
  await clickButton(page, 'Scan and preview')
  await clickButton(page, 'Continue')
  await clickButton(page, 'Continue')
  await clickButton(page, 'Submit job')
  await clickButton(page, 'View diagrams')

  await page.waitForSelector('[aria-label^="Diagram canvas for"] svg', { timeout: 20000 })
  // Wait a frame for initial fit calculation
  await new Promise(r => setTimeout(r, 300))

  const metrics = await page.evaluate(() => {
    const stage = document.querySelector('[aria-label^="Diagram canvas for"]')
    const svg = stage.querySelector('svg')
    const stageRect = stage.getBoundingClientRect()
    const svgRect = svg.getBoundingClientRect()
    const shapes = Array.from(svg.querySelectorAll('g.node rect, g.node polygon, g.node circle, g.node path'))
    const fills = shapes.map(s => getComputedStyle(s).fill)
    const texts = Array.from(svg.querySelectorAll('g.node text, g.node tspan'))
    const textMetrics = texts.map(t => ({
      text: (t.textContent || '').trim(),
      fill: getComputedStyle(t).fill,
      height: t.getBoundingClientRect().height,
    })).filter(t => t.text.length > 0)
    const edgePaths = Array.from(svg.querySelectorAll('g.edgePaths path'))
    const edgeStrokes = edgePaths.map(p => getComputedStyle(p).stroke)
    return {
      stageW: stageRect.width,
      stageH: stageRect.height,
      svgW: svgRect.width,
      svgH: svgRect.height,
      shapeCount: shapes.length,
      fills,
      textMetrics,
      edgeCount: edgePaths.length,
      edgeStrokes,
    }
  })

  assert(metrics.shapeCount >= 2, `Expected >=2 node shapes, got ${metrics.shapeCount}`)
  assert(
    metrics.fills.every(f => f !== 'rgb(0, 0, 0)' && f !== 'none'),
    `Found black or unfilled node shapes: ${JSON.stringify(metrics.fills)}`,
  )
  assert(metrics.textMetrics.length >= 2, `Expected >=2 visible node labels, got ${metrics.textMetrics.length}`)
  assert(
    metrics.textMetrics.every(t => t.height >= 7),
    `Found unreadable tiny node label height: ${JSON.stringify(metrics.textMetrics)}`,
  )
  assert(
    metrics.edgeCount === 0 || metrics.edgeStrokes.some(s => s && s !== 'none'),
    `Expected styled edge strokes, got: ${JSON.stringify(metrics.edgeStrokes)}`,
  )

  // Verify Fit button, Zoom +, and Zoom - work
  await clickButton(page, 'Zoom +')
  const zoomedW = await page.evaluate(() => document.querySelector('[aria-label^="Diagram canvas for"] svg').getBoundingClientRect().width)
  assert(zoomedW > metrics.svgW, `Expected Zoom + to enlarge SVG (${zoomedW} <= ${metrics.svgW})`)

  await clickButton(page, 'Fit')
  const fittedW = await page.evaluate(() => document.querySelector('[aria-label^="Diagram canvas for"] svg').getBoundingClientRect().width)
  assert(fittedW <= metrics.stageW + 2, `Expected Fit to keep SVG within stage (${fittedW} > ${metrics.stageW})`)

  assert(consoleErrors.length === 0, `Unexpected console errors (including CSP): ${consoleErrors.join('; ')}`)
  assert(pageErrors.length === 0, `Unexpected page errors: ${pageErrors.join('; ')}`)
  assert(externalRequests.length === 0, `Unexpected external requests: ${externalRequests.join('; ')}`)

  console.log(JSON.stringify({ ok: true, metrics }))
} finally {
  await browser.close()
}
