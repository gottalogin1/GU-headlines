// Screenshots of the running site, used by the live source check workflow.
// Usage: node screenshots.cjs <base-url> <output-dir>
const { chromium } = require(require.resolve("playwright", { paths: [process.cwd()] }));

const [base, out] = process.argv.slice(2);
const shots = [
  ["home-desktop", "/", { width: 1280, height: 1600 }, "light"],
  ["home-dark", "/", { width: 1280, height: 1000 }, "dark"],
  ["home-phone", "/", { width: 390, height: 1400 }, "light"],
  ["military", "/category/military", { width: 1280, height: 1100 }, "light"],
];

(async () => {
  const browser = await chromium.launch();
  for (const [name, path, viewport, colorScheme] of shots) {
    const page = await browser.newPage({ viewport, colorScheme });
    await page.goto(base + path, { waitUntil: "networkidle" });
    // Make lazy images load before the picture is taken.
    await page.evaluate(() => document.querySelectorAll("img[loading=lazy]").forEach((i) => (i.loading = "eager")));
    await page.waitForLoadState("networkidle");
    await page.screenshot({ path: `${out}/${name}.png` });
    await page.close();
  }
  await browser.close();
})();
