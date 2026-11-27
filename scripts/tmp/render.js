// TEMP: render a page in Chromium and print what its calendar widget loads.
const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const seen = [];
  page.on('response', async (res) => {
    const u = res.url();
    if (/eventscalendar|boomte|inffuse|calendar|ical|\.ics/i.test(u) && !/parastorage|wixstatic/.test(u)) {
      let body = '';
      try { body = (await res.text()).slice(0, 2500); } catch (e) { body = '(no body)'; }
      seen.push(`RESPONSE ${res.status()} ${u}\n${body}\n`);
    }
  });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
  for (let y = 0; y < 8000; y += 600) { await page.mouse.wheel(0, 600); await page.waitForTimeout(400); }
  await page.waitForTimeout(5000);
  for (const f of page.frames()) {
    console.log('FRAME', f.url().slice(0, 400));
    try { const t = await f.evaluate(() => document.body ? document.body.innerText : ''); console.log(t.slice(0, 3000)); } catch (e) {}
  }
  console.log(seen.join('\n'));
  const footer = await page.evaluate(() => (document.querySelector('footer') || document.body).innerText);
  console.log('FOOTER', footer.slice(0, 1500));
  await browser.close();
})();
