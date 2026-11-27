// TEMP: render a page in Chromium and print what its calendar widget loads.
const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const seen = [];
  page.on('response', async (res) => {
    const u = res.url();
    if (/calendar\/v3\/calendars/.test(u)) {
      seen.push(`CAL ${u.replace(/key=[^&]+/, 'key=X')}`);
      try {
        const data = JSON.parse(await res.text());
        for (const it of data.items || []) {
          seen.push(`  ${(it.start || {}).dateTime || (it.start || {}).date} | ${it.summary} | ${it.location || ''} | rec=${it.recurringEventId ? 'y' : 'n'} | ${(it.description || '').replace(/\s+/g, ' ').slice(0, 120)}`);
        }
      } catch (e) { seen.push('  (no json)'); }
    }
    if (/calendar\.google\.com\/calendar\/embed/.test(u)) seen.push(`EMBED ${u}`);
  });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
  for (let y = 0; y < 8000; y += 600) { await page.mouse.wheel(0, 600); await page.waitForTimeout(400); }
  await page.waitForTimeout(5000);
  // Also the next months, in the calendar's own frame.
  const cal = page.frames().find((f) => /calendar\.google\.com/.test(f.url()));
  if (cal) {
    for (let i = 0; i < 3; i++) {
      try { await cal.click('[aria-label*="Next" i]', { timeout: 5000 }); await page.waitForTimeout(3000); } catch (e) { console.log('no next button', e.message.slice(0, 100)); }
    }
  }
  for (const f of page.frames()) {
    console.log('FRAME', f.url().slice(0, 400));
  }
  console.log(seen.join('\n'));
  const footer = await page.evaluate(() => (document.querySelector('footer') || document.body).innerText);
  console.log('FOOTER', footer.slice(0, 1500));
  await browser.close();
})();
