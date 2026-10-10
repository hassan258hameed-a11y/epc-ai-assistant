/**
 * Compile the vendored Tailwind runtime against every rendered page and fail
 * loudly if any of them errors or produces no CSS.
 *
 *   node _dev/tw/check_all.js _dev/out/*.html
 *
 * One bad utility class (for example an off-scale opacity like `bg-brand-500/12`)
 * aborts the whole stylesheet and leaves every page unstyled, so this is the
 * check that catches that class of mistake.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..', '..');
const tailwindSrc = fs.readFileSync(path.join(root, 'static', 'vendor', 'tailwind.js'), 'utf8');

const IGNORE = /should not be used in production/;

function compile(pagePath) {
  return new Promise((resolve) => {
    const html = fs.readFileSync(pagePath, 'utf8')
      .replace(/<script src="\/static\/vendor\/tailwind\.js"><\/script>/, '');

    const dom = new JSDOM(html, {
      runScripts: 'outside-only',
      pretendToBeVisual: true,
      url: 'http://127.0.0.1:8000/',
    });
    const { window } = dom;

    const problems = [];
    let finished = false;                 // ignore anything that lands after we resolve
    const record = (text) => {
      const line = String(text).split('\n')[0].slice(0, 220);
      if (!finished && !IGNORE.test(line)) problems.push(line);
    };

    window.console.error = (...a) => record(a.map(String).join(' '));
    window.console.warn = (...a) => record(a.map(String).join(' '));

    const onRejection = (e) => record((e && e.reason) || (e && e.message) || e);
    process.on('unhandledRejection', onRejection);

    try {
      window.eval(tailwindSrc);
    } catch (e) {
      record('tailwind load: ' + e.message);
    }

    // Page scripts run for completeness, but their failures are not Tailwind's
    // concern — jsdom lacks layout, IntersectionObserver and friends.
    window.document.querySelectorAll('script:not([src])').forEach((tag) => {
      try { window.eval(tag.textContent); } catch (_) { /* not a styling problem */ }
    });

    setTimeout(() => {
      const css = [...window.document.querySelectorAll('style')]
        .filter((s) => !s.getAttribute('type'))
        .map((s) => s.textContent || '')
        .join('');

      // Preflight alone is ~6-9KB, so size proves nothing. A healthy build must
      // contain real utilities and the shared component layer.
      const hasUtilities = /\.flex\{/.test(css) || /\.hidden\{/.test(css);
      const hasComponents = /\.(btn|btn-primary|card|input|nav-link|badge-brand)\{/.test(css);
      if (!hasUtilities) problems.push('no utility classes were generated');
      if (!hasComponents) problems.push('design-system component layer missing');

      finished = true;
      process.removeListener('unhandledRejection', onRejection);
      resolve({ page: path.basename(pagePath), bytes: css.length, problems });
      // Deliberately not calling window.close(): Tailwind keeps async work
      // queued, and tearing the document down makes it throw spuriously.
    }, 3000);
  });
}

(async () => {
  const pages = process.argv.slice(2);
  let failed = 0;

  for (const page of pages) {
    const html = fs.readFileSync(page, 'utf8');
    if (!/tailwind\.config/.test(html)) {
      console.log(`skip ${path.basename(page).padEnd(30)} (not a full page — redirect or fragment)`);
      continue;
    }

    const r = await compile(page);
    const ok = r.problems.length === 0;
    if (!ok) failed++;
    console.log(`${ok ? 'OK  ' : 'FAIL'} ${r.page.padEnd(30)} ${String(r.bytes).padStart(7)} bytes of CSS`);
    r.problems.forEach((p) => console.log('       ' + p));
  }

  console.log(`\n${pages.length} pages checked, ${failed} failing`);
  process.exit(failed ? 1 : 0);
})();
