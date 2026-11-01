/* Resolve Playwright without hard-coding a machine path.

 * Order: PLAYWRIGHT_MODULE (absolute path to the package) -> normal node resolution.
 * Usage from web/*.cjs       : require("../../web/lib/playwright.cjs")
 * Usage from web/legacy/*.cjs: require("../../web/lib/playwright.cjs")
 * Usage from deployment/*.cjs: require("../../web/lib/playwright.cjs")
 */
let playwright;
try {
  playwright = require("../../web/lib/playwright.cjs");
} catch (error) {
  const override = process.env.PLAYWRIGHT_MODULE;
  if (!override) throw error;
  playwright = require(override);
}
module.exports = playwright;
