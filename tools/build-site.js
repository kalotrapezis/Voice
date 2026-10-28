// Builds docs/index.html (the GitHub Pages site) from tools/site.template.html, putting the app's own
// Material icons (web/icons.js) where the template says {{icon:name}}. Usage: node tools/build-site.js
const fs = require('fs'), path = require('path');
const root = path.join(__dirname, '..');
const { icon } = new Function(fs.readFileSync(path.join(root, 'web/icons.js'), 'utf8') + '; return { icon };')();
const html = fs.readFileSync(path.join(__dirname, 'site.template.html'), 'utf8')
  .replace(/\{\{icon:(\w+)\}\}/g, (_, n) => icon(n, 30));
if (html.includes('{{')) throw new Error('unknown placeholder left in the template');
fs.writeFileSync(path.join(root, 'docs/index.html'), html);
console.log('docs/index.html', html.length, 'bytes');
