// Renders the top of the page as the 1200x630 preview card X and other sites show.
import { chromium } from "playwright";

const [file, out] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, colorScheme: "light" });
await page.goto("file://" + file, { waitUntil: "networkidle" });
await page.addStyleTag({ content: ".wrap { padding-block: 56px 0 !important; }" });
await page.screenshot({ path: out, clip: { x: 0, y: 0, width: 1200, height: 630 } });
await browser.close();
