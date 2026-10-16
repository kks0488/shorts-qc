import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';

const base = process.env.VIEWER_URL || 'http://127.0.0.1:8080';
const out = process.env.CAPTURE_DIR || 'media';
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || undefined });
const watchdog = setTimeout(() => { console.error('Browser check timed out'); browser.close().finally(() => process.exit(1)); }, 45000);
try {
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base, { waitUntil: 'networkidle' });
  await page.getByText('Publish with confidence.').waitFor();
  await page.locator('#findings .finding').first().waitFor();
  assert.equal(await page.locator('#caption-status').textContent(), 'DNN CAPTIONS ACTIVE');
  await page.locator('#video').evaluate(async video => {
    if (video.readyState < 1) await new Promise((resolve, reject) => {
      video.addEventListener('loadedmetadata', resolve, {once:true});
      video.addEventListener('error', () => reject(Error('Video decode failed')), {once:true});
    });
    await new Promise(resolve => {
      video.addEventListener('seeked', resolve, {once:true});
      video.currentTime = 9.5;
    });
  });
  await page.waitForFunction(() => document.getElementById('video').readyState >= 3);
  assert.equal(await page.locator('#video').evaluate(video => video.currentTime), 9.5);
  // Present a decoded frame before capturing; a paused seek can precede paint.
  await page.locator('#video').evaluate(async video => {
    await video.play();
    await new Promise(resolve => video.requestVideoFrameCallback(resolve));
    video.pause();
  });
  const videoRange = await page.request.get(`${base}/demo.mp4`, {headers:{Range:'bytes=0-99'}});
  assert.equal(videoRange.status(),206);
  assert.equal((await videoRange.body()).length,100);
  await page.screenshot({ path: `${out}/viewer-desktop.png`, fullPage: true });
  await page.screenshot({ path: `${out}/viewer-preview.png`, fullPage: false });
  await page.locator('#filter').selectOption('review');
  assert(await page.locator('#findings .finding').count() > 0);
  assert.equal(await page.locator('#findings .finding:not(.review)').count(), 0);
  await page.locator('#filter').selectOption('error');
  const first = page.locator('#findings .finding').first();
  await first.click();
  await page.waitForFunction(() => Math.abs(document.getElementById('video').currentTime - 2) < .1);
  await page.screenshot({ path: `${out}/viewer-finding.png`, fullPage: false });
  assert.equal(await page.locator('#findings .finding.review').count(), 0);
  const metrics = await page.locator('#csv-link').getAttribute('href');
  assert.equal((await page.request.get(metrics)).status(), 200);
  await page.locator('#filter').selectOption('all');
  await page.getByText('Analysis limits & responsible use').click();
  assert(await page.locator('#limits li').count() >= 5);
  // A malicious-looking filename stays plain text, never executable markup.
  await page.locator('#file').setInputFiles({name:'report.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({
    ...(await (await page.request.get(`${base}/report.json`)).json()),
    source:{...(await (await page.request.get(`${base}/report.json`)).json()).source,name:'<img src=x onerror=alert(1)>'}
  }))});
  await page.waitForFunction(() => document.getElementById('filename').textContent.startsWith('<img'));
  assert.equal(await page.locator('#filename img').count(),0);
  const mobile = await context.newPage({viewport:{width:390,height:844}});
  await mobile.setViewportSize({width:390,height:844});
  await mobile.goto(base,{waitUntil:'networkidle'});
  await mobile.locator('#findings .finding').first().waitFor();
  assert(await mobile.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await mobile.screenshot({path:`${out}/viewer-mobile.png`,fullPage:true});
  await context.close();
  assert.deepEqual(errors, []);
  console.log('PASS: report load, actual DNN status, video decode/seek, filters, evidence download, limits, safe text rendering, mobile overflow; screenshots captured.');
} finally { clearTimeout(watchdog); await browser.close(); }
