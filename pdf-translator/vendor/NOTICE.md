Vendored library: [pdf.js](https://github.com/mozilla/pdf.js) (`pdfjs-dist`) v4.6.82, Mozilla, Apache License 2.0.

Files `pdf.min.js` and `pdf.worker.min.js` are the unmodified `legacy/build` output from the
`pdfjs-dist` npm package (originally named `pdf.min.mjs` / `pdf.worker.min.mjs`), vendored here so
the app doesn't depend on an external CDN. They are renamed to `.js` only so static hosts always
serve them with a JavaScript MIME type; the contents are byte-identical ES modules.
