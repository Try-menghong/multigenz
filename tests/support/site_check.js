// Loads the download page in Chromium at desktop/phone widths in light and dark and reports what a visitor gets.
// usage: node site_check.js <url>   (needs the `playwright` package; PLAYWRIGHT_BROWSERS_PATH as installed)
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const out = {};
  const views = [["desktop-light", 1280, "light"], ["desktop-dark", 1280, "dark"], ["phone-light", 390, "light"],
                 ["phone-dark", 390, "dark"]];
  for (const [name, width, scheme] of views) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme,
                                           permissions: ["clipboard-read", "clipboard-write"] });
    const page = await ctx.newPage();
    const errors = [];
    page.on("console", m => { if (m.type() === "error" && !/fonts\.(googleapis|gstatic)/.test(m.text())) errors.push(m.text()); });
    page.on("pageerror", e => errors.push(String(e)));
    await page.goto(process.argv[2], { waitUntil: "load" });
    const r = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      cmd: document.getElementById("cmd-text").textContent,
      bg: getComputedStyle(document.body).backgroundColor,
    }));
    await page.click("#copy-btn");
    await page.waitForFunction(() => document.getElementById("copy-btn").textContent !== "Copy");
    r.clipboard = await page.evaluate(() => navigator.clipboard.readText());
    await page.click("#tab-uninstall");
    r.uninstall = await page.textContent("#cmd-text");
    r.errors = errors;
    out[name] = r;
    await ctx.close();
  }
  console.log(JSON.stringify(out));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
